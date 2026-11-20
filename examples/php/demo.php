<?php
/**
 * 命令行示例：把一张图翻成指定语言并落盘。
 *
 * 用法：
 *
 *     # 本地文件 + 默认 JSON 入口（能顺带打印原文/译文对照）
 *     php demo.php --img=../../tests/fixtures/demo_zh.png --lang=en --out=../../out/php_en.png
 *
 *     # 只想要图，走二进制端点（省内存，适大图）
 *     php demo.php --img=../../tests/fixtures/demo_zh.png --lang=ja --out=../../out/php_ja.png --mode=file
 *
 *     # 传远程 URL
 *     php demo.php --img=https://cdn.example.com/a.jpg --lang=ko --out=out/ko.png --mode=file
 *
 *     # 其余可选项
 *     --source=ja          源语言（默认中文）
 *     --base=http://127.0.0.1:8000   服务地址，默认取 IMGTRANS_BASE_URL
 *     --key=xxx             覆盖 IMGTRANS_API_KEY
 *
 * 退出码：0 成功；1 业务错误（E_*）；2 用法错误。方便在脚本/CI 里判断。
 */

require_once __DIR__ . '/ImgTransClient.php';

$opts = getopt('', array('img:', 'lang:', 'out::', 'source::', 'base::', 'key::', 'mode::'));

if (!isset($opts['img'], $opts['lang'])) {
    fwrite(STDERR, "用法: php demo.php --img=<路径或URL> --lang=<语言码> [--out=输出路径] [--mode=json|file]\n");
    exit(2);
}

$img = (string) $opts['img'];
$lang = (string) $opts['lang'];
$mode = isset($opts['mode']) ? (string) $opts['mode'] : 'json';
$source = isset($opts['source']) ? (string) $opts['source'] : null;

$base = isset($opts['base']) ? (string) $opts['base'] : getenv('IMGTRANS_BASE_URL');
if (!$base) {
    $base = 'http://127.0.0.1:8000';
}

$key = isset($opts['key']) ? (string) $opts['key'] : getenv('IMGTRANS_API_KEY');
if (!$key) {
    fwrite(STDERR, "缺少 API Key：用 --key=... 传入，或设置环境变量 IMGTRANS_API_KEY\n");
    exit(2);
}

$out = isset($opts['out']) ? (string) $opts['out'] : null;
if ($out === null) {
    $out = __DIR__ . '/out_' . $lang . '.png';
}

try {
    $client = new ImgTransClient($base, $key, array('timeout' => 180));
    $started = microtime(true);

    if ($mode === 'file') {
        // 二进制端点：本地文件走 multipart，URL 走 JSON+二进制
        $meta = $client->translateToFile($img, $lang, $out, $source);
        $elapsed = microtime(true) - $started;
        printf("已写出 %s\n", $meta['path']);
        printf("  mime=%s  块数=%s  已回填=%s  服务端耗时=%sms  本地耗时=%.1fs\n",
            $meta['mime'],
            $meta['blocks'] === null ? '-' : $meta['blocks'],
            $meta['painted'] === null ? '-' : $meta['painted'],
            $meta['total_ms'] === null ? '-' : number_format($meta['total_ms'], 0),
            $elapsed
        );
        printf("  文件大小=%.1f KB\n", filesize($meta['path']) / 1024);
        exit(0);
    }

    if ($mode !== 'json') {
        fwrite(STDERR, "--mode 只支持 json 或 file\n");
        exit(2);
    }

    // JSON 入口只收 URL 或 base64，本地路径在这里编码
    $payload = preg_match('#^https?://#i', $img) === 1 ? $img : base64_encode(file_get_contents($img));
    $res = $client->translate($payload, $lang, $source);
    $elapsed = microtime(true) - $started;

    $binary = base64_decode($res['image_base64'], true);
    if ($binary === false || $binary === '') {
        throw new ImgTransException('image_base64 解码失败', 'E_CLIENT');
    }
    if (@file_put_contents($out, $binary) === false) {
        throw new ImgTransException('无法写入 ' . $out, 'E_CLIENT');
    }

    printf("已写出 %s\n", $out);
    printf("  %s  %dx%d  %d 个文字块  服务端耗时=%sms  本地耗时=%.1fs\n",
        $res['mime'], $res['width'], $res['height'], count($res['blocks']),
        number_format($res['total_ms'], 0), $elapsed
    );
    if (!empty($res['warnings'])) {
        printf("  警告: %s\n", implode('; ', $res['warnings']));
    }

    printf("\n%-4s %-6s %s\n", '#', '回填', '原文 -> 译文');
    printf("%s\n", str_repeat('-', 72));
    foreach ($res['blocks'] as $block) {
        $flag = $block['painted'] ? '是' : '否';
        printf("%-4s %-6s %s -> %s\n", $block['index'], $flag, $block['source'], $block['target']);
        if (empty($block['painted']) && !empty($block['note'])) {
            printf("     (未回填原因: %s)\n", $block['note']);
        }
    }
    exit(0);
} catch (ImgTransException $e) {
    fwrite(STDERR, sprintf(
        "翻译失败 [%s] HTTP %d: %s%s\n",
        $e->getErrorCode(),
        $e->getHttpStatus(),
        $e->getMessage(),
        $e->isRetryable() ? '（可重试）' : '（重试无意义，先改参数/配置）'
    ));
    exit(1);
} catch (Exception $e) {
    fwrite(STDERR, '未预期错误: ' . $e->getMessage() . "\n");
    exit(1);
}
