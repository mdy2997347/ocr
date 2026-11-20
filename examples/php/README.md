# PHP 客户端示例

给业务侧（PHP / OpenCart / 自研后台）直接调用的最小封装，**纯 curl，无框架依赖，PHP >= 7.2**。

```
examples/php/
├── ImgTransClient.php   客户端类（单文件，可整文件复制进项目）
├── demo.php             命令行示例，验证链路是否通
└── README.md            本文件
```

## 60 秒上手

```php
require 'ImgTransClient.php';

$client = new ImgTransClient('http://127.0.0.1:8000', '你的 IMGTRANS_API_KEY');

// 只要图：二进制直写文件，不走 base64
$meta = $client->translateToFile('/data/a.png', 'en', '/data/a.en.png');

// 还要原文/译文对照：JSON 入口
$res = $client->translate('https://cdn.example.com/a.jpg', 'ja');
file_put_contents('/data/a.ja.png', base64_decode($res['image_base64']));
```

命令行自测（`--base` / `--key` 不传则读环境变量 `IMGTRANS_BASE_URL` / `IMGTRANS_API_KEY`）：

```bash
php demo.php --img=../../tests/fixtures/demo_zh.png --lang=en --out=out_en.png
php demo.php --img=../../tests/fixtures/demo_zh.png --lang=ja --out=out_ja.png --mode=file
php demo.php --img=https://cdn.example.com/a.jpg --lang=ko --out=out_ko.png --mode=file
```

## 两种模式怎么选

| | `translate()` | `translateToFile()` |
|---|---|---|
| 端点 | `POST /v1/translate/img` | URL → `/v1/translate`；本地文件 → `/v1/translate/upload` |
| 入参 | URL 或 base64 | URL 或本地路径 |
| 返回 | 完整 JSON（含 `blocks` 原文/译文） | 图片直接落盘 + 元信息 |
| 内存 | base64 双向膨胀约 33% | 流式写文件，`CURLOPT_FILE` |
| 适用 | 需要双语对照、要存译文文本 | 只要图、图很大 |

`translateToFile()` 会自动按入参形态选端点：本地文件走 multipart（**完全不经过 base64**），
URL 走 JSON 入、二进制出。两者都先用 `$outPath.part` 写入、成功后才改名，避免业务读到半截文件。

## 方法一览

```php
new ImgTransClient(string $baseUrl, string $apiKey, array $options = []);
//   options: timeout（默认 180 秒）/ connect_timeout（默认 5 秒）

$client->health();                                   // ['ok'=>true,'device'=>'cpu']，免鉴权
$client->languages();                                // ['targets'=>[...35 个], 'count'=>35]
$client->translate($img, $lang, $source = null);     // 返回完整 JSON 数组
$client->translateToFile($img, $lang, $outPath, $source = null);
//   返回 ['path','mime','blocks','painted','total_ms']
```

## 错误处理

所有失败都抛 `ImgTransException`，带服务端错误码：

```php
try {
    $meta = $client->translateToFile($path, $lang, $out);
} catch (ImgTransException $e) {
    error_log(sprintf('[imgtrans] %s HTTP %d %s', $e->getErrorCode(), $e->getHttpStatus(), $e->getMessage()));

    if ($e->isRetryable()) {
        // 网络抖动 / OCR 偶发失败 → 值得重试
    } else {
        // E_AUTH / E_LANG / E_PARAM / E_PAYLOAD / E_CLIENT → 改参数或配置，重试没用
    }
}
```

| 错误码 | HTTP | 含义 |
|---|---|---|
| `E_AUTH` | 401 | key 没带或不对 |
| `E_LANG` | 400 | `lang` 不在 35 个白名单里（如 `zh`） |
| `E_PARAM` | 400 | `img` 既不是 URL 也不是合法 base64 |
| `E_IMAGE_FETCH` | 400 | URL 下载失败 / 内网地址被 SSRF 防护拦下 |
| `E_PAYLOAD` | 413 | 图片超过 20 MB |
| `E_OCR` / `E_INTERNAL` | 500 | 服务端推理或未预期异常，可重试 |
| `E_CURL` | — | 客户端网络层失败（连不上、超时），`getHttpStatus()` 是 curl errno |
| `E_CLIENT` | — | 客户端参数/IO 问题（文件不存在、目录不可写） |

## 部署注意事项

1. **超时按分钟给**。CPU 单图 12~20 秒，curl 默认超时会把长任务掐断，表现成"服务没响应"。
   默认 180 秒是刻意的，别往下调。
2. **不要靠并发提吞吐**。服务端默认单 worker，OCR 推理串行，并发请求只会排队，
   还可能把 PHP 的 `max_children` 占满。要吞吐就多部署实例。
3. **大图别走 base64**。20 MB 图编码后 27 MB，`memory_limit` 容易被打穿；用
   `translateToFile()` 的本地文件模式（multipart）。
4. **目标语言清单里没有中文**。只能「中文 → 外语」；`lang=zh` 会返回 `E_LANG`。

## 已验证

本示例在 **PHP 7.4.30（宝塔 Windows 版，curl/openssl/mbstring 已启用）** 上实跑通过：

| 场景 | 结果 |
|---|---|
| JSON 模式，640×400 中文图 → en | 4 块全部回填，13.6 s，输出 `SHA1 839689f4…`（与 CLI 输出一致） |
| multipart 模式 → ja | 块数 4 / 已回填 4，16.7 s，`SHA1 ccea9377…`（与 Python 客户端输出**逐字节相同**） |
| URL 入参 + 二进制端点 | 正常落盘 8.9 KB |
| 错 key / 非法语言 / 文件不存在 / 服务未启动 | 分别 `E_AUTH` 401、`E_LANG` 400、`E_CLIENT`、`E_CURL` errno 7，退出码符合约定 |

PHP 7.2 与 7.4 之间本示例只用到 7.1 就有的语法（可空类型、`CURLFile`），未使用箭头函数、
类型属性等 7.4+ 特性。
