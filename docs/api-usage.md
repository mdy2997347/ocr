# 图片翻译 API 调用说明

面向接入方（PHP / Node / Python 均可）。核心是一个接口：**传`lang` + `img`，拿回 JSON，里面是翻译后图片的 base64**。

## 一、启动服务

```bash
cd /path/to/ocr
python scripts/serve.py                    # 读 .env（key / 代理 / 端口 / 设备）
python scripts/serve.py --port 8000 --workers 1
```

启动后先确认服务和配置：

```bash
curl -s http://127.0.0.1:8000/healthz
# {"ok":true,"device":"cpu"}
```

`/healthz` **不需要鉴权**，适合做探活；`device` 是实际使用的推理设备（`cpu` / `gpu:0`）。
生产环境把它接到网关的健康检查上即可。

`.env` 里与调用相关的只有两项：

```ini
IMGTRANS_API_KEY=<40 位随机串>   # 调用方请求头要带这个
IMGTRANS_PROXY=                  # 服务器能直连出海就留空
```

## 二、接口一览

| 方法 | 路径 | 请求体 | 响应 | 用途 |
|---|---|---|---|---|
| GET | `/healthz` | — | JSON | 探活，**免鉴权** |
| GET | `/v1/languages` | — | JSON | 取支持的 35 个目标语言码 |
| POST | `/v1/translate/img` | JSON `{lang, img}` | **JSON** | **推荐接入用** |
| POST | `/v1/translate` | JSON `{url, target}` | 二进制图片 | 老接口，URL 入、图片出 |
| POST | `/v1/translate/upload` | multipart `file + target` | 二进制图片 | 老接口，文件入、图片出 |

新接入统一用 `/v1/translate/img`：请求体是 JSON、响应固定 JSON，不用根据
`?detailed=1` 之类的开关决定怎么解析。

### 鉴权

请求头二选一，两种都认：

```
X-Api-Key: <IMGTRANS_API_KEY>
Authorization: Bearer <IMGTRANS_API_KEY>
```

不传或传错 → `401` + `{"error":{"code":"E_AUTH",...}}`。

### `/v1/translate/img` 请求字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `lang` | string | 是 | 目标语言码，见 `/v1/languages`；`EN`、`en-US`、`pt_BR` 会自动归一 |
| `img` | string | 是 | `http(s)://` 图片地址，**或** base64 字符串（可带 `data:image/png;base64,` 前缀，允许 76 字符折行） |
| `source` | string | 否 | 源语言码；不传按默认 OCR 语言（中文）识别 |

上限：图片 **20 MB**（base64 字符串本身可达 ~27 MB，因为 base64 有 33% 膨胀）。

## 三、调用示例

### 1. URL 传入（最省事，适合图片已在 CDN）

```bash
KEY=$(grep '^IMGTRANS_API_KEY=' .env | cut -d= -f2)

curl -s -X POST http://127.0.0.1:8000/v1/translate/img \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $KEY" \
  -d '{"lang":"en","img":"https://example.com/banner.jpg"}' \
  -o resp.json
```

注意：URL 由**服务端**下载，所以服务器的 `IMGTRANS_PROXY` 必须能出海（Google 翻译也要走它）。
内网地址（`127.0.0.1`、`169.254.169.254` 等）会被 SSRF 防护直接拒绝，返回 `E_IMAGE_FETCH`。

### 2. base64 传入（本地已有文件）

```bash
python - <<'PY' > req.json
import base64, json, pathlib
b = pathlib.Path("tests/fixtures/demo_zh.png").read_bytes()
print(json.dumps({"lang": "en", "img": base64.b64encode(b).decode()}))
PY

curl -s -X POST http://127.0.0.1:8000/v1/translate/img \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $KEY" \
  --data-binary @req.json -o resp.json
```

实测（640×400 的演示图，冷启动）：请求体 609 KB，响应 583 KB，`HTTP 200`，耗时 12.9 s。

### 3. PHP

**推荐的写法是用现成客户端**（`examples/php/ImgTransClient.php`，单文件、纯 curl、PHP >= 7.2，
已在本机 PHP 7.4 上实跑，输出与 CLI 逐字节一致）：

```php
require 'ImgTransClient.php';

$client = new ImgTransClient('http://127.0.0.1:8000', getenv('IMGTRANS_API_KEY'));

// 只要图：本地文件走 multipart，不经过 base64，大图也不炸内存
$meta = $client->translateToFile('/data/a.png', 'en', '/data/a.en.png');
printf("块数 %d，已回填 %d，服务端 %sms\n", $meta['blocks'], $meta['painted'], $meta['total_ms']);

// 还要原文/译文对照：JSON 入口
$res = $client->translate($b64OrUrl, 'ja');
file_put_contents('/data/a.ja.png', base64_decode($res['image_base64']));
foreach ($res['blocks'] as $b) {
    echo $b['source'], ' -> ', $b['target'], "\n";
}
```

出错统一抛 `ImgTransException`，`getErrorCode()` 拿服务端错误码，`isRetryable()` 判断
该不该重试（`E_AUTH`/`E_LANG`/`E_PARAM` 重试没意义）。完整用法见 `examples/php/README.md`。

**不想引入文件的话**，最小内联版（只演示 JSON 入口）：

```php
$payload = json_encode(['lang' => $lang, 'img' => base64_encode(file_get_contents($path))]);
$ch = curl_init('http://127.0.0.1:8000/v1/translate/img');
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => $payload,
    CURLOPT_HTTPHEADER     => ['Content-Type: application/json', 'X-Api-Key: ' . $key],
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_TIMEOUT        => 180,   // OCR 是秒级，别用默认超时
]);
$body  = curl_exec($ch);
$code  = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

$data = json_decode((string) $body, true);
if ($code !== 200) {
    throw new RuntimeException($data['error']['message'] ?? 'translate failed', $code);
}
file_put_contents($outPath, base64_decode($data['image_base64']));
```

`json_encode` 默认把中文转成 `\uXXXX`，服务端能正常解析，不必加 `JSON_UNESCAPED_UNICODE`。

### 4. Python

```python
import base64, requests

with open("demo.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

r = requests.post(
    "http://127.0.0.1:8000/v1/translate/img",
    headers={"X-Api-Key": key},
    json={"lang": "ja", "img": b64},
    timeout=300,
)
r.raise_for_status()
data = r.json()
open("out.png", "wb").write(base64.b64decode(data["image_base64"]))
```

实测中→日：`HTTP 200`，4 个文字块全部回填，耗时 18.5 s（含冷启动加载模型）。

## 四、响应字段

成功固定返回（三个入口形状一致）：

```json
{
  "image_base64": "iVBORw0KGgoAAAANSUhEUg...",
  "mime": "image/png",
  "width": 640,
  "height": 400,
  "blocks": [
    {
      "index": 0,
      "source": "限时优惠",
      "target": "Limited time offer",
      "painted": true,
      "font_size": 49,
      "lines": ["Limited time offer"],
      "fit_mode": "expand",
      "note": ""
    }
  ],
  "timings": {"decode": 27.1, "ocr": 11681.6, "group": 0.3, "translate": 793.9,
              "mask": 7.1, "erase": 48.5, "render": 193.0, "encode": 45.2},
  "total_ms": 12804.9,
  "warnings": []
}
```

| 字段 | 说明 |
|---|---|
| `image_base64` | 翻译后图片，**需要自己 base64 解码再存**；字段名带 `_base64` 就是提醒这个 |
| `mime` | 输出格式，由 `PipelineConfig.output_format` 决定（默认 `image/jpeg`） |
| `blocks[].source` / `.target` | 识别到的原文 / 译文，**已按段落聚合**，可直接落库做双语对照 |
| `blocks[].painted` | 是否真的画进图里；`false` 表示识别到但没回填（无译文、块太小等） |
| `blocks[].note` | `painted=false` 时的原因，排查用 |
| `blocks[].fit_mode` | 字号调整策略（`shrink` / `expand` / `expand+expand_v`） |
| `timings` / `total_ms` | 各阶段耗时（ms），`ocr` 通常占 90% 以上 |
| `warnings` | 非致命问题，例如图片过大、字体回退 |

调用方只需要 `image_base64`；`blocks` 和 `timings` 是赠送的可观测性数据。

## 五、错误码

所有错误统一为：

```json
{"error": {"code": "E_LANG", "message": "不支持的目标语言: 'zh'；支持 35 种: en, ar, ..."}}
```

| code | HTTP | 触发场景 | 调用方应对 |
|---|---|---|---|
| `E_AUTH` | 401 | 没带 key / key 不对 | 检查配置，不要重试 |
| `E_LANG` | 400 | `lang` 不在 35 个白名单里（如传 `zh`） | 改参数，不要重试 |
| `E_PARAM` | 400 | `img` 既不是 URL 也不是合法 base64、解码后为空 | 改参数 |
| `E_IMAGE_FETCH` | 400 | URL 下载失败（404 / 超时 / 内网地址被拦） | 换图源，可重试 |
| `E_PAYLOAD` | 413 | 图片超过 20 MB | 先压图 |
| `E_OCR` | 500 | OCR 推理失败 | 可重试一次 |
| `E_INTERNAL` | 500 | 未预期异常 | 可重试，持续失败看服务日志 |

**语言码校验发生在下载之前**（实测钉住的行为）：传 `lang=zh` + 一个 404 的 URL，返回的是
`E_LANG` 而不是 `E_IMAGE_FETCH`。这样按错误码排查时不会被图片问题误导。

## 六、集成注意事项

1. **超时按分钟设**。CPU 单图 12~20 s（`ocr` 阶段占 11 s 以上），冷启动再加 6~8 s。
   PHP 的 `CURLOPT_TIMEOUT` 建议 180 s，网关默认 60 s 会直接切断。
2. **服务默认单 worker，OCR 推理是串行的**。并发请求会排队，不是并行加速。
   要提吞吐靠多实例 + 前置负载均衡（每实例各自加载一份模型，内存按 2 GB/实例算）。
3. **base64 双向膨胀**。20 MB 图 → 27 MB 请求体；响应同理。PHP 侧注意
   `memory_limit` 与 `post_max_size`（本接口是 JSON body，`post_max_size` 会拦）。
4. **重试是安全的**：接口无状态、不落盘，同图重复调用结果一致（已实测 HTTP 与 CLI 输出
   SHA1 完全相同）。但重试前先看 `code`，`E_AUTH` / `E_LANG` / `E_PARAM` 重试没有意义。
5. **目标语言里没有中文**。35 个目标码是业务方指定的清单，不含 `zh`——即当前只能
   「中文 → 外语」和「外语 → 外语」，**不能把外文图翻成中文**，传 `lang=zh` 会得到
   `E_LANG`。渲染侧的中文字体已就绪，真要开放只需在 `imgtrans/translator/lang_map.py`
   的 `SUPPORTED_LANGS` 里加 `zh`（源侧 `zh` 已经可用）。
6. **源语言默认中文**。非中文原图要显式传 `source`（如 `{"lang":"en","source":"ja"}`），
   否则会用中文 OCR 模型去读，识别率会明显下降。
7. **字体缺失不会报错**。目标语言（泰/印地/格鲁吉亚等）没有对应字体时，
   图里的字会变成豆腐块 □，接口照样返回 200。部署后跑一次
   `python scripts/doctor.py` 核对每种文字实际命中的字体。
