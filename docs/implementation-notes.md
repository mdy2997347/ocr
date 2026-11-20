# 图片翻译服务 · 实现记录

> 配套文档：`docs/image-translate-technical-design.md`（设计方案）
> 本文记录的是**代码落地时的实测结果**，尤其是"设计里没料到、真实跑起来才发现"的问题。
> 日期：2026-11-19

## 一、当前可运行范围

| 环节 | 状态 | 说明 |
|---|---|---|
| 图片下载 + SSRF 防护 | ✅ 已实现 | 拦内网/云元数据地址（`169.254.169.254` 等） |
| OCR | ✅ 已实现 | PaddleOCR 3.7.0 + PP-OCRv6_medium（det/rec）+ PP-LCNet 方向分类；**行级结果聚合为段落** |
| 翻译 | ✅ 已实现 | 移植版 Google 双通道（`client=te` 主 / `client=at` 降级）；**目标语言硬白名单 35 种**，非法码入口即拒 |
| 文字擦除 | ✅ 已实现 | 四级策略：纯色 → 平面拟合 → inpaint → 降级 |
| 样式还原 | ✅ 已实现 | 前景/背景色、描边、字重、对齐、RTL |
| 回填渲染 | ✅ 已实现 | 4x 超采样、**字形高度反推字号 + 同图字号归一化**、横向+纵向扩框、短标签不折行、拉丁词不断词 |
| HTTP 接口 | ✅ 已实现 | FastAPI，**JSON 入口 `/v1/translate/img`**（`{lang,img}`，img 支持 URL 与 base64）+ URL + multipart 三入口，统一错误契约；`GET /v1/languages` 返回目标语言清单；`.env` 提供 API Key |
| 真实联网翻译 | ✅ 已验证 | 中→英/日/韩/俄/阿 均实测通过 |
| 推理设备 | ✅ 已实现 | `.env` 的 `IMGTRANS_DEVICE`（**默认 cpu**）+ `--device` 覆盖；`auto` 有卡用卡、无卡回落；配了 gpu 但用不了则启动即失败（`E_DEVICE`）；`/healthz` 回报实际设备 |
| Linux 部署 | ✅ 已核查 | 代码零 Windows 依赖；字体目录改为递归扫描（Linux 是多层目录，原实现索引为 0）；`.ttc` 按语言族选 face；`scripts/doctor.py` 一键自检。详见 `docs/linux-deploy.md` |
| 独立部署 | ✅ 已实测 | uv + `pyproject.toml`/`uv.lock`：依赖、模型（`IMGTRANS_MODEL_DIR`）、解释器全部收进项目目录，删目录即卸载；CPU/GPU 用互斥 extras 挡住"装一个默默卸掉另一个"。详见 `docs/uv-isolated-deploy.md` |

**测试**：268 项单元测试全绿（翻译层 95 / 图像链路 42 / OCR 与 pipeline 26 / 段落聚合 17 / API 契约 22 / 设备解析 26 / 字体发现 17 / 配置 14 / 其他 9）。

**接入文档**：`docs/api-usage.md`（字段表、curl / PHP / Python 示例、错误码对照、超时与并发注意事项）；
PHP 客户端见 `examples/php/`（`ImgTransClient.php` + `demo.php`，仅依赖 curl 扩展，PHP >= 7.2）。

## 二、实测踩到的坑（按影响排序）

### 1. 颜色通道顺序：黄色被画成蓝色

**现象**：中文海报的黄色标题 `#FFEC78`，翻译后变成浅蓝色。不抛异常、测试全绿、只能靠肉眼看出来。

**根因**：`BoxAnalysis.fg_color` / `bg_color` 的类型标注是 `RGB`，但取值来自 OpenCV 的 BGR 图，实际是 BGR 顺序。两处下游用法相反：

- `render/text.py` 拿它当 RGB 填 Pillow 的 RGBA 画布 → 画出来通道颠倒；
- `erase.py` 把它直接写进 BGR 图像 → 反而是对的。

**修法**：统一成「对外一律 RGB」，在 `mask.py` 出圈时翻转，在 `erase.py` 落笔前翻回 BGR。两侧各加一条注释说明这个约定。

**教训**：三个通道都是 0~255 的整数，顺序错了**永远不会报错**。必须有断言钉住（`test_mask.ColorChannelOrderTest`）。

### 2. OCR 语言按目标语言推断

**现象**：翻韩语时整张图原样返回，警告"所有块译文与原文一致或无有效译文"。

**根因**：`resolve_ocr_lang(source_lang or target_lang)` —— 目标语言是韩语，就用韩语 OCR 模型去读中文原图，一个字都认不出来（6 个块全丢）。

**修法**：OCR 语言只看源语言；未指定时用 `ch`（PP-OCRv6 单模型覆盖中/英/日 + 46 种拉丁语系，召回最广）。另加一条兜底：默认模型零检出时，用 `latin` 再试一次。

### 3. MKLDNN 与 PIR 执行器不兼容（直接崩）

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
not support [pir::ArrayAttribute<pir::DoubleAttribute>]
(at ..\paddle\fluid\framework\new_executor\instruction\onednn\onednn_instruction.cc:118)
```

`paddlepaddle 3.3.1` + `enable_mkldnn=True`（默认）时，检测阶段直接崩。**必须 `enable_mkldnn=False`**，已在 `OcrEngine.__init__` 里默认关闭并写明原因。

关掉后 640×400 图：模型加载约 6~8s，纯推理 5.4~6.3s（CPU，i7 级别）。

### 4. 擦除分级判据用错了指标

**现象**：所有块都走 `cv2.inpaint`（慢，大区域有涂抹感），包括本该平面拟合的渐变背景。

**根因**：判据用的是背景像素的**原始 std**。线性渐变上下能差几十个灰度，std 轻松 30+，被误判成"纹理"。但渐变其实是完美可拟合的。

**修法**：改用**平面拟合残差** `picture_residual_std()`。实测同一张图上：渐变块残差 ≈ 4（正好是噪点水平），红色按钮块 27.8（跨了红蓝边界）——区分度非常干净。

**附加坑**：采样环必须**外扩**再取。OCR 框紧贴文字，直接用框内的外圈采样，环上落的就是笔画本身，残差会被顶到 20+。

### 5. 字号被压得太小（译文长度膨胀）

**现象**：`限时优惠`（4 字）→ `Limited time offer`（18 字符），只缩字号时从 52px 压到 27px；正文更惨，20px → 9px，几乎不可读。

**修法**：新增**横向扩框**（`_available_width`），顺序改为「先争取空间，再缩字号」。三重约束取最严：

1. 图像边界；
2. 同层相邻块（只看 y 方向有重叠的，上下相邻不算）；
3. **视觉内容**：逐列看 Canny 边缘密度，超阈值就停 —— OCR 认不出的图标/装饰线只有边缘能发现。

效果：同一张图上标题 27px → 49px，正文 9px → 18px。

**关键细节**：边缘图必须在**擦除后**的底图上算，否则本块原文的边缘会把自己挡住。

### 6. 灰度 Canny 对「等亮度不同色」无效

**现象**：`立即购买` 译文溢出红色按钮右边界，画到了蓝色背景上。

**根因**：红底 `(220,60,70)` 与深蓝背景 `(69,99,162)` 的灰度分别是 **109 和 97，只差 12**，灰度 Canny 完全看不到这条边。

**修法**：边缘图改成三张取并集 —— 灰度 + LAB 的 `a` 通道 + `b` 通道。色度通道对红蓝交界很敏感。

### 7. 阿拉伯语方向反了（缺 bidi）

**现象**：阿拉伯语译文读不起来——单词顺序和字母顺序都是倒的，且溢出图片左边界。

**根因**：`Pillow` 的 `features.check("raqm")` 为 `False`，没有布局引擎，阿拉伯文按字符原样从左到右画。

**修法**：`arabic_reshaper.reshape()`（转连通形态）+ `bidi.algorithm.get_display()`（双向重排），之后按 LTR 绘制即得正确视觉顺序。同时 RTL 文字的**扩展方向改为向左**（右端锚在原框右缘），对齐方式强制右对齐。

### 8. TKK 动态抓取已彻底失效

**实测**：Google 翻译首页（3.1MB，页面日期 2026-11-17）**已经完全不含 `tkk` 变量**，不是正则写错。

**影响**：现在全靠内置常量 `406398.2087938574` 工作（实测服务端仍接受）。一旦 Google 轮换 TKK，将**没有自愈路径**，只能靠探针提前发现。

**处置**：默认 `fetch_tkk=False`，不再每进程白下 3MB 首页 + 白等一次 connect 超时。翻译阶段耗时从 4.5s 降到 1.3s。开关保留，万一 Google 恢复可立刻打开。

配套：`scripts/verify_live.py` 在线探针，退出码非 0 即故障，建议 cron 每 10 分钟跑一次。

### 9. OCR 是**行级**的，按行翻译会把一句话切碎

**现象**：拿一张真实的电商科普长图（790×831）跑，一段 4 行的正文被拆成 4 个独立块，
译出来是 4 个残缺句子：`…becomes liquid and can greatly` / `Wet the interface thoroughly…` /
`out; when the device is in…`。同一段内字号 9px 与 25px 并存——**尾句恰好短，
在 25px 下也放得下，于是被画成了副标题大小**。

**根因**：PaddleOCR 的检出粒度是文本行，"一段话折成几行"就是几个互不相干的框。
按行送翻译，跨块的主谓宾和从句全断；按行回填，每块各自以自身行高起步再缩字号。

**修法**：OCR 之后、翻译之前加**段落聚合**（`imgtrans/ocr/group.py`）。
四条件同时满足才合并，宁可少合并也不误合并：

1. 竖直间距 ≤ 0.62 × 行高；
2. 确实在下一行（顶部至少低 0.30 × 行高，排除"同一行被切成左右两块"）；
3. 水平重叠 ≥ 45%（相对较窄的一行，缩进两格仍满足）；
4. 行高差 ≤ 40%（字号不同多半是嵌了小字注释）。

合并后 `TextBlock.line_height` 记住**单行**行高，回填用它推导初始字号——
用整段高度起步会先得到一个几倍大的字号再被一路缩到 9px，等于没改。

效果：17 块 → 12 块，三段正文各成一段，字号 11/17/18 → 20/17/18，译文语义完整。

### 10. 纵向也要扩框（只扩横向是不够的）

**现象**：第一段原文 1 行、译文 2 行，框高只够 1 行，字号被压到 11px，而它**下方就是
46px 的段落间距**——白白空着却把字缩了。

**修法**：新增 `_available_height`，与横向扩框同构（画布边界 / 下方最近的块 /
逐行看 Canny 边缘密度），但有两条额外约束，都是血腥教训：

* **只向下扩，不向上扩**。向上会压住上一段，且与阅读方向相反。文字超出原框时
  绘制改为**顶部锚定**，否则默认的"上下各溢出一半"会顶到上一段文字上。
* **必须留段间距**（0.45 × 行高）。第一版没留，三段正文直接粘成一坨、
  第二段的末尾与第三段开头只剩 8px——比字号小一点难受得多。
* 画布底部也要留白，否则最后一行会被裁掉半截（实测最底部那行标题被切）。

### 11. 擦除残影：字形下缘根本不在 mask 里

**现象**：标题黄底黑字擦完，每个字的**下缘**留一排断续黑点。

**根因**：不是填充色错，是 **mask 没覆盖到**。PaddleOCR 的框（`y1=96`）比字形实际
下缘（≈100）短了 4px，那几行像素压根不在 ROI 内，`mask` 自然是 0。

**修法**：`analyze_box` 的**分析区域**按 `MASK_PAD_LEFT/TOP/RIGHT=1`、
`MASK_PAD_BOTTOM=4` 非对称外扩（中文字形视觉重心偏上，收笔基本在下方）。
外扩后参考环必须再从**外扩后的区域**往外让出 `RING_WIDTH`，否则环会落在
新扩进来的字迹上，把背景色估计污染成文字色。

**为什么只扩这么点**：外扩会让 ROI 跨出背景块（黄带扩到蓝底），跨出去的部分
与背景差异巨大，会被 Otsu 当成前景擦掉。所以外扩量必须小到"跨不出去"。

### 12. 极小的框不该回填（照片内嵌小字）

**现象**：三张小缩略图里的英文标注 `Room`（框高 7px）、`Compound Softens`（11px）
被 OCR 认出并翻译，日文译文在 9px 最小字号下仍放不下，溢出压到照片内容上，
**把缩略图弄脏了**。

**处置**：`PipelineConfig.min_block_height = 12.0`，低于此值的块不送翻译、不擦除、
保留原文。这类框装不下可读的译文，重画必然破坏图像——不翻译是唯一体面的选择。

**顺带**：`图二`→`写真` 这类 2 字标签在 25px 宽的框里被拆成上下两行，像排版事故。
加 `_is_short_label`（纯 CJK ≤ 4 字 / 单个拉丁词 ≤ 12 字符）**禁止折行**，
缩到 12px 单行即可。

### 13. 段落边界不能只靠几何：段内行距和段间距可以一样大

**现象**：第二张图（790×918，白底正文 + 电路板照片）**零合并**——六块正文各自为战，
字号 12 / 15 / 15 / 18 / 16 / 27 / 30px，同一段话里像混排了标题和脚注。

**根因**：这张图是紧凑排版，实测段内行距 0.68 × 行高、**段间距 0.73 × 行高**——
两者只差 1~2 像素，任何几何阈值都分不开。原阈值 0.62 甚至把段内行也挡住了。

**修法**：几何只做粗筛（放宽到 0.90），真正的段末判定交给**行尾标点**：
上一行以 `。！？；` 结尾就不再与下一行合并。中文折行时标点跟字走，
句号几乎只出现在段末，逗号/顿号才是行中断点。误判方向也是安全的——
少合并只是没优化，多合并会把两段话拼成一句且字号被统一成错的值。

效果：13 块 → 9 块，正文三段各自成段。

### 14. 字号应该由**字形高度**反推，不是框高

**现象**：同一张图里正文 23px、下一段 28px、强调句 30px，而原文通篇 22px。
第一张图的标题也一直偏小（黄带里空着一圈）。

**根因**：初始字号一直用 `box_h * 1.05`。但 OCR 框高含上下留白，比真实字高大
25~35%；多行段落合并后框高更是整段高度，能大出一倍多。按框高定字号，
误差方向和幅度都不可控。

**修法**：`BoxAnalysis.text_height` 记录笔画 mask 实测的**单行**字形高度，
用它反推字号。两个坑：

* **多行段落不能取墨迹总高**——里面含着行间距，反推出来偏大一整倍。
  要按行投影切出连续墨迹带（`_single_line_height`），过滤掉标点那种很矮的带，
  取中位带高。
* 单块测量本身有 ±20% 噪声（框贴合程度、抗锯齿、括号等高字符），
  所以再加一道 `_stabilize_sizes`：同图内落在中位数 `0.82~1.20` 窗口里的块
  统一到中位数——真实排版里同级文字字号必然一致，标题/脚注因超出窗口而不受影响。

### 15. 拉丁单词被拦腰截断

**现象**：右侧竖排区译文挤成 `Originato` / `r of` / `phase`。

**根因**：`wrap_text` 对超宽 token 一律逐字符切。对 CJK 长串这是唯一办法，
但对拉丁词是灾难——断词是可读性问题，溢出只是美观问题。

**修法**：含 CJK 的 token 仍逐字符切；纯拉丁词整词独占一行，允许横向溢出。

### 16. 贴边被裁的行：不翻译，但要擦掉

**现象**：图顶部有一行只剩半截的中文，OCR 硬猜成"霍尼书小的 PTM7950 是 790 余列中的
高等热柏变材"，译出来 `Honey Book ... hot cypress material`——错得离谱。

**处置**：判定为"贴着上边缘 + 高度 < 0.7 × 同图中位行高"的块，不送翻译。
但**要擦掉**：一行残缺中文留在英文图里比空白更扎眼，而它本身不可译。
为此擦除阶段单独带一批 `erase-only` 的分析结果，不进入渲染列表。

**顺带一个自伤**：过滤时用了 `h = b.box[3] - b.box[1]`，把外层的**图像高度** `h`
遮蔽了，后面 `np.zeros((h, w))` 建成"一块文字那么高"的 mask，直接
`operands could not be broadcast together`。变量名改成 `bh`。

### 17. 目标语言做硬白名单，而不是"透传给 Google 让它报错"

**背景**：业务定下 35 个目标语言码（比原 PHP 表多 `hr`，且**不含中文**——中文只作源）。

**为什么不裸传**：PHP 原版任何字符串都直接拼进 `tl=`。拼错（`jp`、`zh-CN`、`korean`）
的后果是远端 400，或者在 `client=te` 上返回**与输入等长但全是原文**的结果，
看起来像"翻译没生效"，排查要从下载、OCR、翻译一路试错。而且这类错误发生在
CPU 上 11~17 秒的 OCR **之后**，用户的等待全部白付。

**做法**：

* `lang_map.SUPPORTED_LANGS` 是唯一权威清单，`AUTO_LIST` 由它生成（再补检测器别名）；
* `normalize_lang()` 负责归一：大小写、空格、`pt_BR`、`en-US`（退到主语言子标签）、
  旧码 `iw`/`in`；归一不出来即不支持；
* `pipeline.translate_bytes()` **第一行**校验，抛 `UnsupportedLangError`（`E_LANG` → HTTP 400），
  错误信息里直接列出全部 35 个码，调用方不用翻文档；
* `GET /v1/languages` 把清单暴露成接口，避免 PHP 侧再维护一份语言表（双写必然不一致）。

**中文只作源语言**这一点在代码里是显式的：`AUTO_LIST` 保留 `zh-Hans` / `zh-Hant`
（供 `is_supported_source` 判断原图语种），但 `SUPPORTED_LANGS` 里没有 `zh`，
所以 `target=zh-CN` 会被拒。**如果以后要支持"译回中文"，只需往 `SUPPORTED_LANGS` 加一项。**

**顺带修的两个字体坑**：

* `ka`（格鲁吉亚文）、`bn`（孟加拉文）此前都会回落到中文字体 → 整片豆腐块。
  新增 `georgian`（Sylfaen）/ `bengali`（Nirmala UI）两个字体族。
* 天城文/孟加拉文的候选名原先写成 `Nirmala.ttc`，而 Windows 上是 `Nirmala.ttf`，
  导致索引永远查不中、静默回退。文件名写错不会报错，只会悄悄画错——这类
  "静默降级"是字体层最难发现的问题。
* 实测本机 `features.check("raqm") == False`：印地语/孟加拉语的**连写整形**会不正确
  （字母能出来，但辅音簇与元音符位置错）。部署机需要带 libraqm 的 Pillow。

### 18. 对外接口：JSON 进 JSON 出，Key 放 `.env`

**需求**：PHP 侧要一个"传 `lang` + `img`、返回 JSON、请求头带 API Key"的接口，
并且项目里要有 `.env` 存 key。

**为什么单独开一个入口而不是改造老的 `/v1/translate`**：老入口的形态是
"**成功返回图片二进制**"（对齐百度图片翻译，PHP `file_get_contents` 直接拿图），
这个契约已有调用方在用，改成 JSON 会直接破坏它。新入口 `/v1/translate/img` 只做
JSON，两者并存、各服务一类调用方。

**`img` 一个字段吃两种形态**：http(s) 地址走 `pipeline.translate_url()`（复用
downloader 的 SSRF 防护、体积限制、Content-Type 校验），其余当 base64 解。
支持 `data:image/png;base64,` 前缀和 76 字符折行——这两种是前端 `FileReader` 和
PHP `base64_encode(chunk_split())` 的**默认产物**，不兼容的话接入方第一步就卡住。

**JSON 形状复用 `?detailed=1`**：三个入口用同一个 `_detail_payload()`，调用方只写
一套解析。``image_base64`` 而不是 ``image``，省掉一轮"这串是什么"的沟通。

**鉴权**：`X-Api-Key` 与 `Authorization: Bearer` 都认（后者是多数 HTTP 客户端的
默认写法，只认一种必然有人 401 后来问）。比较用 `secrets.compare_digest`，
不用 `==`——key 校验是暴力破解入口，不该留时序侧信道。

**`.env` 为什么不引 `python-dotenv`**：只需解析 `KEY=VALUE`，多一个依赖就多一次
生产装包失败的机会。`imgtrans/env.py` 二十行搞定，支持注释、`export`、成对引号、BOM。

三条语义是被测试钉住的：

* **默认不覆盖**已存在的环境变量——优先级 `命令行 > 环境变量 > .env`，
  线上临时改值不用动文件；
* **找不到文件安静跳过**，不抛异常打断启动；
* **库导入时不自动加载**：`imgtrans` 被测试和 CLI 共用，``import`` 时偷改
  `os.environ` 会让测试互相污染。只有 `scripts/serve.py` / `scripts/translate_image.py`
  这两个入口显式调 `load_env()`。

**一个踩过的坑**：`argparse` 的 `default` 是**构建 parser 时**求值的。先建 parser
再 `load_env()`，`--port` 的默认值就永远是硬编码的那个，`.env` 里的端口形同虚设。
顺序必须是「先 `load_env()`，后 `ArgumentParser()`」。

**实测**（真实图片，750×1000 电商产品图）：

| 调用方式 | 结果 |
|---|---|
| URL + `X-Api-Key` | 200，15.2s，返回 694 KB base64 |
| base64 + `Authorization: Bearer` | 200，11.8s |
| 无 key / 错 key | 401 `E_AUTH` |
| `lang=zh-CN` | 400 `E_LANG` |
| `img` 传内网地址 | 400 `E_IMAGE_FETCH`（下载器拦截） |

HTTP 输出与 CLI 输出 **SHA1 完全一致**（`afbbf7a8…`），说明两条路径没有分叉。

### 19. 推理设备：默认 cpu，配置了才用卡

**需求**：有 GPU 时在 `.env` 里配，默认 cpu，程序里动态读取。

**为什么默认 `cpu` 而不是"自动探测有卡就用卡"**：自动挑卡最典型的翻车是
一台显存被训练任务占满的机器——服务启动时抢不到显存，报一个和配置完全无关的
paddle 内部错误。默认值必须是"最不容易误解"的那个，想有卡用卡就显式写 `auto`。

**配置优先级**：`--device` 命令行 > `IMGTRANS_DEVICE` 环境变量 > `.env` > 默认 cpu。
解析统一走 `imgtrans/device.py::resolve_device()`，接受 `cpu` / `gpu` / `gpu:1` /
`cuda` / `cuda:0` / `auto`，大小写和空格不敏感。写错（`gpu0`、`gpu:abc`）直接抛
`E_DEVICE`，**绝不猜**——"猜对了"会掩盖拼写错误，"猜错了"才是灾难。

**配了 gpu 但用不了时直接失败，不静默降级**：CPU 版 paddle 收到 `device="gpu:0"`
要么抛一个信息量极低的内部异常，要么在某些版本上悄悄跑回 CPU。后者最坏——运维
以为在吃卡，实际慢十几倍，性能问题查一周都查不出来。现在构造 `OcrEngine` 时就
探测（`is_compiled_with_cuda()` + `device_count()`），不可用则启动即退出，并把
"当前 paddle 3.3.1 是 CPU 版"写进错误信息，同时给出 `auto` 这条退路。

**探测成本**：`cpu` 分支只读配置、不 `import paddle`，避免为了一个默认值拖慢启动。

**顺带两个细节**：GPU 上强制关掉 `enable_mkldnn`（oneDNN 是 CPU 后端，传 True
只会误导）；`GET /healthz` 返回最终生效的 `device` —— "到底跑在卡上还是 CPU"
是最常被问、也最容易被 `.env` 与实际环境不一致搞混的一件事。

**实测**（本机 paddle 3.3.1 为 CPU 版）：

| 配置 | 结果 |
|---|---|
| 不配 / `cpu` | 正常启动，`/healthz` → `{"ok":true,"device":"cpu"}` |
| `gpu` | 退出码 2，报错含"当前 paddle 3.3.1 是 CPU 版" |
| `auto` | 日志写明"回落到 cpu"，正常启动 |

### 20. `--no-warmup` 形同虚设：预热被调了两次

`serve.py` 在 `factory()` 里预热一次，`create_app()` 的 `startup` 钩子里**又无条件
预热一次**。于是 `--no-warmup` 只挡住了前者，服务照样卡 3.5s 等模型加载——参数
写了等于没写，比没这个参数更糟（人会以为是自己哪儿配错了）。

修法：`create_app(warmup=...)` 显式传下去，`factory()` 里那次删掉，预热只由
生命周期钩子负责。实测 `--no-warmup` 启动从 7.2s 降到 1.4s，日志出现
`跳过模型预热（warmup=False）`。

**教训**：同一个副作用有两个触发点，加开关时必须两个都改；"参数生效了但没有
完全生效"比"参数不支持"更难排查。

### 21. 移植到 Linux：字体目录只扫一层，索引直接为 0

**需求**：检查能否跨平台部署到 Linux。

**代码层其实没毛病**：路径全用 `pathlib`、文本 IO 显式 `utf-8-sig` 或二进制、
无 `winreg`/`msvcrt`/`fcntl`、61 条包内导入的文件名大小写精确一致（Linux 区分大小写），
22 个依赖 pin 全部在 PyPI 存在且关键二进制包都有 `manylinux` wheel（含 cp313）。

**但字体层是致命的。** Windows 的字体目录是扁平的
（`C:\Windows\Fonts\msyh.ttc`），Linux 是两层的
（`/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`）。原实现 `iterdir()` 只扫
一层，在 Linux 上扫到的全是 `truetype/`、`opentype/` 这类**目录**，被 `is_file()`
过滤掉。用临时目录模拟两种布局实测：

| 布局 | 索引数 | `find("zh")` |
|---|---|---|
| 两层嵌套（Linux 真实布局） | **0** | `None` |
| 扁平（Windows） | 4 | 命中 |

索引为 0 的后果不是报错，而是 `ImageFont.load_default()` —— Pillow 的 10px
位图字体，整张图所有译文都糊掉。这种"能跑、能出图、但完全不可用"的失败，
在容器里很可能被当成"渲染质量差"而不是"缺字体"。

修法：`os.walk` 递归（深度上限 4、不跟随符号链接防环、目录与文件名排序保证
跨机一致），系统目录按 `sys.platform` 分支，并新增 `IMGTRANS_FONT_DIRS`
指定容器里的自定义字体目录。

**第二个 Linux 专有坑**：`fonts-noto-cjk` 的 `NotoSansCJK-Regular.ttc` 把
JP/KR/SC/TC/HK 五个字面打进一个文件，**face 0 是日文**。中文目标取 face 0 会
得到日式字形（「直」「骨」「关」写法不同），普通用户只会觉得"哪里怪"。
新增 `_face_index()` 按族名 token 探测 face 序号；探测失败一律返回 0（旧行为），
所以最坏等于没做，不会引入回归。

**第三个（顺手）**：回退日志分级。以前一律 `warning`，现在"中日韩回退到 latin"
升级为 `error`（必然豆腐块），"日韩回退到 CJK"降为 `debug`（CJK 含假名，正常）。
级别不分，Linux 上少装字体的真问题会被当成"字体不好看"忽略。

**交付**：`scripts/doctor.py` 一键自检（依赖版本 / raqm / **每种文字实际命中哪个
字体文件** / 推理设备 / 配置 / 目录可写，`FAIL` 时退出码 1），以及
`docs/linux-deploy.md`（系统包、Dockerfile、systemd、故障对照表）。

**镜像瘦身**：`torch`/`torchvision` 代码里 0 处引用（擦除走 OpenCV + PIL，
未启用 LaMa），Linux 上跳过可省 2 GB+；但 `shapely`/`pyclipper`/`scipy`
虽然是间接使用，却是 paddleocr 的传递依赖，不能删。

### 22. 语言校验排在下载之后：错误码会被图片问题顶掉

`/v1/translate/img` 传 URL 时，`translate_url()` 是**先 `fetch_image()` 再进
`translate_bytes()`**，而语言白名单校验在 `translate_bytes()` 里。后果：传
`{"lang":"zh","img":"https://.../404.jpg"}` 返回的是 `E_IMAGE_FETCH`（HTTP 404），
不是 `E_LANG`。

这个错误码不只影响观感——调用方按 `code` 排查，`E_IMAGE_FETCH` 会把人的注意力
引到图片地址上，而真正的错在 `lang`。写调用文档时实测到，属于文档逼出来的 bug。

修法：`translate_url()` 入口先 `_require_target_lang()`，再下载。`translate_bytes()`
里的校验保留（幂等、开销可忽略），这样两个入口各自都自洽。

**测试**：`test_img_json_validates_lang_before_download` 同时断言 `E_LANG` 和
`fetch_image` **没有被调用**——只断言错误码的话，将来有人把顺序改回去仍会绿。

### 23. 二进制出口的两个端点，元信息头不一致

`/v1/translate` 回了 `X-ImgTrans-Blocks` / `Painted` / `Total-Ms` 三个头，
`/v1/translate/upload` 只回了 `Total-Ms`。写 PHP 客户端时 `blocks` 一直是 `null`，
才反过来查到是服务端少给了——**两个出口各写一份 `Response(...)` 就会这样**。

改为共用一个 `_binary_response(result, elapsed_ms)`，并在上传口测试里断言三个头都在。
这类"复制了一份但没同步"的问题，只靠看代码很难发现，得有个真实调用方去消费它。

**顺带**：PHP 示例不是照文档抄的，是真跑出来的——本机宝塔 PHP 7.4.30 上，
multipart 模式的输出与 Python 客户端输出 **SHA1 完全相同**（`ccea9377…`），
JSON 模式与本项目 CLI 输出相同（`839689f4…`）。语言校验顺序的 bug（第 22 条）
也是写这个调用方时才暴露的。

### 24. 用 uv 做独立部署：三个"看起来配了其实没生效"的陷阱

目标很具体：让服务连同模型、依赖、解释器都收进一个目录，删目录即卸载，
同机上别的 Python 项目完全不受影响。结论是能做，但踩了三个静默失败。

**① 手写 lock 的版本冲突，只有真实解析器能发现。**
`requirements.lock.txt` 里写的是 `numpy==2.5.5`，而 `paddlex 3.7.x` 声明
`numpy>=1.24,<2.4`。`uv` 直接报 `No solution found`，`pip` 装到一半也会崩。
这份 lock 是"按版本核实结果预填"的，不含传递依赖，本质不可复现。
改为 `numpy>=1.24,<2.4`（实测落 2.3.5），并把真源让给 `pyproject.toml` + `uv.lock`。

**② `PADDLE_PDX_CACHE_HOME` 必须在 import paddle 之前设置。**
`paddlex/utils/cache.py` 是模块级求值：

```python
CACHE_DIR = os.environ.get("PADDLE_PDX_CACHE_HOME", DEFAULT_CACHE_DIR)
```

也就是说，`import paddlex` 之后再 `os.environ[...] = ...` **完全无效**，而且不报错，
模型照旧写到 `~/.paddlex`。这个坑的恶劣之处在于：配置检查全对、日志也显示配了，
只有看磁盘才发现理解错了。所以 `apply_model_dir()` 必须紧跟 `load_env()`，
早于任何 paddle 导入（本项目的引擎是惰性导入，正好来得及）。

**③ 相对路径按 cwd 解析会把 240 MB 建到奇怪的地方。**
`IMGTRANS_MODEL_DIR=.models` 这种写法如果按 cwd 解析，systemd 的
`WorkingDirectory` 一旦不是项目根，模型就落到 `/` 或 `~` 下——下次启动换个 cwd
又重新下一份。改为锚定 `default_env_path.parent`（项目根）。

另外两件实测确认的事：

- **`uv sync --extra cpu --extra gpu` 会直接报错**。`paddlepaddle` 与
  `paddlepaddle-gpu` 互斥，pip 时代是"装一个默默卸掉另一个"，装完环境半坏且不报错。
  `[tool.uv] conflicts` 把这种事挡在解析阶段。
- **国内直连 PyPI 会失败，且错误被吞**。`uv sync` 不给 `--default-index` 时，
  在本机表现为"没有任何输出、退出码 1"。加阿里云镜像后 59 秒装完 98 个包。
  写文档时不写这条，接手的人会以为 uv 坏了。

**验证方式**（不是"装完能用"就算过）：把 `~/.paddlex/official_models` 整个改名移走，
只留 `.env` 里一行 `IMGTRANS_MODEL_DIR=.models`，程序仍跑通且输出 SHA1 与全局环境
完全一致（`839689f4…`）。这排除了"其实还在偷偷用全局模型"。

**体量参考**：`.venv` 765 MB（paddle 368 / cv2 161 / pandas 40）、模型 140 MB、
`uv sync` 59 秒、`uv lock` 2.1 秒解析 98 个包。方案详见 `docs/uv-isolated-deploy.md`。

## 三、实测性能（CPU，无 GPU）
640×400 海报，6 个文字块：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| OCR | 11~17s | 含模型加载 6~8s；纯推理 5.4~6.3s。**GPU 上是数量级差距** |
| 翻译 | 1.2s | 6 块一次请求（多 `q` 批量） |
| mask + 擦除 | 70ms | 平面拟合为主，只有按钮块走 inpaint |
| 渲染 | 150~310ms | 4x 超采样，逐块 |
| 编码 | 46ms | PNG 无损 |
| **合计** | **13~20s** | CPU 开发机；GPU 部署时 OCR 会降到亚秒级 |

翻译 20 次连续请求实测全部 200，无限流，约 1.1 req/s（受跨境 RTT ~0.9s 限制）。

## 四、已知限制

1. **竖排文字**：只擦不画（`renderable=False`），会留空白。
2. **超长译文**：阿拉伯语这类比中文长 2~3 倍的语种，在窄框里仍会被压到 9~10px。横向+纵向扩框已实现，但**物理空间不足时无解**（如 55px 宽的图例条要塞 17 个日文字符），这类只能接受或跳过回填。
3. **艺术字 / 曲面文字 / 手写体**：OCR 阶段就会失败或框不准。
4. **半透明水印、密集小字**：mask 分割会把水印当文字擦掉。
5. **RTL 混排**：阿拉伯语与数字/英文混排时 `get_display` 的结果在个别标点上可能不理想。
6. **超大图**：> 4000×4000 只告警，未做分块。
7. **CPU 部署**：单图 15~20s，不适合在线服务；生产必须有 GPU。

## 五、怎么跑

**推荐用 uv（环境、依赖、模型全部收在项目目录内）**：

```bash
uv sync --extra cpu                 # 首次建 .venv（GPU 机器用 --extra gpu）
uv run --no-sync python scripts/serve.py
uv run --no-sync python scripts/doctor.py
```

`.venv` 765 MB、模型 140 MB 都在项目目录里，删目录即彻底卸载；
同机其它 Python 项目不受影响（`numpy` 这类窄区间依赖尤其重要）。
完整方案（含离线部署、systemd、与 Docker 的取舍）：`docs/uv-isolated-deploy.md`。

下面是不用 uv 的老路径，等价：

```bash
# 单图端到端（需要能访问 Google 的出口）
python scripts/translate_image.py \
    --input tests/fixtures/demo_zh.png \
    --to en \
    --proxy http://127.0.0.1:10809 \
    --out out.png

# 只看识别与翻译结果，不落盘
python scripts/translate_image.py --input a.png --to ja --dry-run

# 在线探针（判断翻译通道是否可用）
python scripts/verify_live.py --proxy http://127.0.0.1:10809

# HTTP 服务（API Key / 代理 / 端口 / 推理设备都从 .env 读）
python scripts/serve.py
# 接口怎么调、返回什么、错误码怎么处理：见 docs/api-usage.md

# 临时覆盖：命令行 > 环境变量 > .env
python scripts/serve.py --device gpu:0
python scripts/serve.py --no-warmup        # 跳过模型预热，起服务只用 1.4s

# 确认这套服务到底跑在哪儿
curl -s http://127.0.0.1:8000/healthz     # {"ok":true,"device":"cpu"}
```

**用 GPU 的三步**：装 GPU 版 paddle（见第六节）→ `.env` 里写
`IMGTRANS_DEVICE=gpu`（想按机器自适应就写 `auto`）→ `curl /healthz` 确认。
配了 `gpu` 但 paddle 还是 CPU 版时服务会**直接退出**并说明原因，不会悄悄跑 CPU。

```bash
# 部署自检（换机器、上容器前先跑：逐语言报告字体命中 / raqm / 设备 / 配置）
python scripts/doctor.py          # 人读；--json 给 CI
python scripts/doctor.py --json

# 全量测试
python -m unittest discover -s tests
```

## 六、依赖安装的三个坑

1. **`pip` 索引解析失败**：本机 `pip 26.1.2` 对清华源返回的 PEP 691 JSON 解析不出任何版本（`Could not find a version that satisfies...`），但 `curl` 访问同一地址是 200。改用腾讯云 / 阿里云源即可，**不是网络问题**。
2. **`paddlepaddle` 不能用 `--no-deps` 装**：会缺 `networkx` / `opt-einsum==3.3.0` / `safetensors`，报错是 `No module named 'google'`（缺 protobuf），跟真实原因无关。

另外，装 `paddlex` 会把 `opencv-python-headless 5.1` 替换成 `opencv-contrib-python 4.10`（并把 numpy 降到 2.3.5）。这反而规避了 5.x 的 API 变更风险，但**部署时不要手动升回 5.x**。

3. **GPU 版要单独装 `paddlepaddle-gpu`**：`paddlepaddle`（CPU 版）和 `paddlepaddle-gpu` 是互斥的两个包，装其中一个会把另一个卸掉。CUDA 版本对应的安装索引见设计文档 §附录。装完用 `python -c "import paddle; print(paddle.device.is_compiled_with_cuda())"` 确认是 `True`，再设 `IMGTRANS_DEVICE=gpu`——服务启动时会做同样的校验，装错了直接拒绝启动。
