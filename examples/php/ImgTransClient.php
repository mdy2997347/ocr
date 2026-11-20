<?php
/**
 * imgtrans 图片翻译服务 —— PHP 客户端。
 *
 * 环境要求：PHP >= 7.2，启用 curl 扩展（openssl 用于 https，finfo 可选）。
 * 不依赖任何框架，单文件即可复制到业务项目里使用。
 *
 * 典型用法：
 *
 *     require 'ImgTransClient.php';
 *     $client = new ImgTransClient('http://127.0.0.1:8000', getenv('IMGTRANS_API_KEY'));
 *
 *     // 只要图（二进制直写文件，不经过 base64，省内存）
 *     $meta = $client->translateToFile('/data/a.png', 'en', '/data/a.en.png');
 *
 *     // 还要原文/译文对照（JSON 入口，返回 blocks）
 *     $res = $client->translate('https://cdn.example.com/a.jpg', 'ja');
 *     file_put_contents('/data/a.ja.png', base64_decode($res['image_base64']));
 *
 * 设计说明：
 *
 * - 接口地址与 key 从构造函数传入，不写死在类里——线上/测试环境用配置注入。
 * - 超时默认 180s：单图 OCR 要 12~20s（CPU），curl 默认超时会把长任务掐断
 *   并表现成"服务没响应"，这是接入时最常见的误判。
 * - 出错统一抛 {@see ImgTransException}，带服务端错误码（E_LANG/E_AUTH/…），
 *   调用方按码分流即可，不用去解析字符串。
 */

/**
 * 服务端返回的业务错误，或客户端侧的网络/IO 错误。
 *
 * `$errorCode` 与服务端 `{"error":{"code":...}}` 保持一致（E_AUTH / E_LANG /
 * E_PARAM / E_IMAGE_FETCH / E_PAYLOAD / E_OCR / E_INTERNAL）；
 * 客户端自身产生的用 E_CURL（网络层）与 E_CLIENT（参数、文件不可写等）。
 */
class ImgTransException extends RuntimeException
{
    /** @var string */
    private $errorCode;

    /** @var int */
    private $httpStatus;

    /**
     * @param string $message
     * @param string $errorCode
     * @param int    $httpStatus
     */
    public function __construct($message, $errorCode = '', $httpStatus = 0)
    {
        parent::__construct((string) $message, (int) $httpStatus);
        $this->errorCode = (string) $errorCode;
        $this->httpStatus = (int) $httpStatus;
    }

    /** @return string 服务端错误码，或 E_CURL / E_CLIENT */
    public function getErrorCode()
    {
        return $this->errorCode;
    }

    /** @return int HTTP 状态码；网络层错误时为 curl errno */
    public function getHttpStatus()
    {
        return $this->httpStatus;
    }

    /**
     * 这个错误重试有没有意义。
     *
     * 参数错、鉴权错、超体积重试一万次也是一样的结果；网络抖动和 OCR 失败
     * 才值得重试。把判断放在异常里，调用方不必各自背一份错误码清单。
     *
     * @return bool
     */
    public function isRetryable()
    {
        return !in_array(
            $this->errorCode,
            array('E_AUTH', 'E_LANG', 'E_PARAM', 'E_PAYLOAD', 'E_CLIENT'),
            true
        );
    }
}

/**
 * 图片翻译客户端。
 */
class ImgTransClient
{
    /** @var string 形如 http://127.0.0.1:8000，末尾斜杠会被去掉 */
    private $baseUrl;

    /** @var string 与 .env 里 IMGTRANS_API_KEY 一致 */
    private $apiKey;

    /** @var int 单次请求总超时（秒） */
    private $timeout;

    /** @var int 连接超时（秒） */
    private $connectTimeout;

    /**
     * @param string $baseUrl 服务地址，例如 http://127.0.0.1:8000
     * @param string $apiKey  IMGTRANS_API_KEY
     * @param array  $options timeout（默认 180）/ connect_timeout（默认 5）
     *
     * @throws InvalidArgumentException 地址或 key 为空
     */
    public function __construct($baseUrl, $apiKey, array $options = array())
    {
        $baseUrl = rtrim(trim((string) $baseUrl), '/');
        $apiKey = trim((string) $apiKey);
        if ($baseUrl === '') {
            throw new InvalidArgumentException('baseUrl 不能为空');
        }
        if ($apiKey === '') {
            throw new InvalidArgumentException('apiKey 不能为空（对应 .env 的 IMGTRANS_API_KEY）');
        }
        $this->baseUrl = $baseUrl;
        $this->apiKey = $apiKey;
        $this->timeout = isset($options['timeout']) ? (int) $options['timeout'] : 180;
        $this->connectTimeout = isset($options['connect_timeout'])
            ? (int) $options['connect_timeout']
            : 5;
    }

    /**
     * 探活。**不需要鉴权**，适合放进业务侧的健康检查。
     *
     * @return array{ok:bool,device:string}
     */
    public function health()
    {
        return $this->getJson('/healthz');
    }

    /**
     * 取支持的目标语言码（当前 35 个）。
     *
     * @return array{targets:string[],count:int}
     */
    public function languages()
    {
        return $this->getJson('/v1/languages');
    }

    /**
     * 翻译并返回完整 JSON（含每个文字块的原文/译文）。
     *
     * 走 `/v1/translate/img`。$img 只接受 **URL 或 base64 字符串**；
     * 手上是本地文件、又只想要图，请用 {@see translateToFile()}，别在这里
     * 手动 base64——20MB 的图会先多占 27MB 内存。
     *
     * @param string      $img    http(s) 地址，或 base64（可带 data: 前缀）
     * @param string      $lang   目标语言码，见 languages()
     * @param string|null $source 源语言码；不传按中文识别
     *
     * @return array image_base64 / mime / width / height / blocks / timings / total_ms / warnings
     *
     * @throws ImgTransException
     */
    public function translate($img, $lang, $source = null)
    {
        $payload = array('lang' => (string) $lang, 'img' => (string) $img);
        if ($source !== null && $source !== '') {
            $payload['source'] = (string) $source;
        }

        return $this->postJson('/v1/translate/img', $payload);
    }

    /**
     * 翻译并把结果**直接写进文件**，返回元信息。
     *
     * 根据 $img 的形态自动选端点：
     * - URL      → `POST /v1/translate`（JSON 入、二进制出）
     * - 本地路径 → `POST /v1/translate/upload`（multipart，**没有 base64 膨胀**）
     *
     * 两个都是二进制响应，用 `CURLOPT_FILE` 边收边写，大图不占内存。
     * 代价是拿不到 blocks 明细（那些只在 JSON 入口里有）——需要对照文本时
     * 用 {@see translate()}。
     *
     * 写文件先落到 `$outPath.part`，成功后才改名，避免半截文件被业务读到。
     *
     * @param string      $img    http(s) 地址，或本地文件路径
     * @param string      $lang   目标语言码
     * @param string      $outPath 输出文件路径（目录不存在会自动创建）
     * @param string|null $source 源语言码
     *
     * @return array path / mime / blocks / painted / total_ms
     *
     * @throws ImgTransException
     */
    public function translateToFile($img, $lang, $outPath, $source = null)
    {
        $img = (string) $img;
        $outPath = (string) $outPath;
        if ($img === '') {
            throw new ImgTransException('img 不能为空', 'E_CLIENT');
        }
        if ($outPath === '') {
            throw new ImgTransException('outPath 不能为空', 'E_CLIENT');
        }

        $dir = dirname($outPath);
        if (!is_dir($dir) && !@mkdir($dir, 0775, true) && !is_dir($dir)) {
            throw new ImgTransException('无法创建输出目录: ' . $dir, 'E_CLIENT');
        }

        $tmp = $outPath . '.part';

        if (preg_match('#^https?://#i', $img) === 1) {
            $payload = array('url' => $img, 'target' => (string) $lang);
            if ($source !== null && $source !== '') {
                $payload['source'] = (string) $source;
            }

            return $this->sendToFile('/v1/translate', array(
                'json' => $payload,
            ), $tmp, $outPath);
        }

        if (!is_file($img)) {
            throw new ImgTransException('图片不存在或不是文件: ' . $img, 'E_CLIENT');
        }

        $fields = array(
            'target' => (string) $lang,
            'file' => new CURLFile($img, $this->mimeOf($img), basename($img)),
        );
        if ($source !== null && $source !== '') {
            $fields['source'] = (string) $source;
        }

        return $this->sendToFile('/v1/translate/upload', array(
            'multipart' => $fields,
        ), $tmp, $outPath);
    }

    // ---------------- 内部实现 ----------------

    /**
     * GET JSON（/healthz、/v1/languages 免鉴权，带上 key 也无害）。
     *
     * @param string $path
     *
     * @return array
     *
     * @throws ImgTransException
     */
    private function getJson($path)
    {
        $ch = $this->newHandle($path);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        $raw = curl_exec($ch);
        $errno = curl_errno($ch);
        $error = curl_error($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($raw === false) {
            throw new ImgTransException('请求失败(' . $errno . '): ' . $error, 'E_CURL', $errno);
        }
        $data = json_decode($raw, true);
        if ($status !== 200) {
            throw $this->buildError($status, $raw, $data);
        }

        return is_array($data) ? $data : array();
    }

    /**
     * POST JSON，返回解析后的数组。
     *
     * @param string $path
     * @param array  $payload
     *
     * @return array
     *
     * @throws ImgTransException
     */
    private function postJson($path, array $payload)
    {
        $body = json_encode($payload, JSON_UNESCAPED_UNICODE);
        if ($body === false) {
            throw new ImgTransException('请求体编码失败: ' . json_last_error_msg(), 'E_CLIENT');
        }

        $ch = $this->newHandle($path);
        curl_setopt_array($ch, array(
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => array(
                'Content-Type: application/json',
                'X-Api-Key: ' . $this->apiKey,
                // 空 Expect 关掉 100-continue：大 body 时它会多一个往返，
                // 部分网关还会因为它把请求挂住。
                'Expect:',
            ),
        ));
        $raw = curl_exec($ch);
        $errno = curl_errno($ch);
        $error = curl_error($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($raw === false) {
            throw new ImgTransException('请求失败(' . $errno . '): ' . $error, 'E_CURL', $errno);
        }
        $data = json_decode($raw, true);
        if ($status !== 200) {
            throw $this->buildError($status, $raw, $data);
        }
        if (!is_array($data)) {
            throw new ImgTransException('响应不是合法 JSON: ' . mb_substr((string) $raw, 0, 200), 'E_CLIENT', $status);
        }

        return $data;
    }

    /**
     * 发二进制响应请求，边收边写到 $tmp，成功后改名到 $outPath。
     *
     * @param string $path
     * @param array  $mode 'json' => array payload | 'multipart' => array fields
     * @param string $tmp
     * @param string $outPath
     *
     * @return array
     *
     * @throws ImgTransException
     */
    private function sendToFile($path, array $mode, $tmp, $outPath)
    {
        $fp = @fopen($tmp, 'wb');
        if ($fp === false) {
            throw new ImgTransException('无法写入临时文件: ' . $tmp, 'E_CLIENT');
        }

        $headers = array();
        $ch = $this->newHandle($path);
        curl_setopt($ch, CURLOPT_FILE, $fp);
        curl_setopt($ch, CURLOPT_HEADERFUNCTION, function ($handle, $line) use (&$headers) {
            $pos = strpos($line, ':');
            if ($pos !== false) {
                $headers[strtolower(trim(substr($line, 0, $pos)))] = trim(substr($line, $pos + 1));
            }

            return strlen($line);
        });

        if (isset($mode['multipart'])) {
            // 不给 Content-Type：交给 curl 生成带 boundary 的头，
            // 手写会把 boundary 弄丢，服务端直接 422。
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_POSTFIELDS, $mode['multipart']);
            curl_setopt($ch, CURLOPT_HTTPHEADER, array(
                'X-Api-Key: ' . $this->apiKey,
                'Expect:',
            ));
        } else {
            $body = json_encode($mode['json'], JSON_UNESCAPED_UNICODE);
            if ($body === false) {
                fclose($fp);
                @unlink($tmp);
                throw new ImgTransException('请求体编码失败: ' . json_last_error_msg(), 'E_CLIENT');
            }
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
            curl_setopt($ch, CURLOPT_HTTPHEADER, array(
                'Content-Type: application/json',
                'X-Api-Key: ' . $this->apiKey,
                'Expect:',
            ));
        }

        $ok = curl_exec($ch);
        $errno = curl_errno($ch);
        $error = curl_error($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $contentType = (string) curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
        curl_close($ch);
        fclose($fp);

        if ($ok === false) {
            @unlink($tmp);
            throw new ImgTransException('请求失败(' . $errno . '): ' . $error, 'E_CURL', $errno);
        }

        if ($status !== 200) {
            // 失败时服务端把 JSON 错误体写进了临时文件，读出来转成异常。
            $raw = is_file($tmp) ? (string) file_get_contents($tmp) : '';
            @unlink($tmp);
            throw $this->buildError($status, $raw, json_decode($raw, true));
        }

        $this->moveFile($tmp, $outPath);

        return array(
            'path' => $outPath,
            'mime' => $contentType,
            'blocks' => isset($headers['x-imgtrans-blocks']) ? (int) $headers['x-imgtrans-blocks'] : null,
            'painted' => isset($headers['x-imgtrans-painted']) ? (int) $headers['x-imgtrans-painted'] : null,
            'total_ms' => isset($headers['x-imgtrans-total-ms']) ? (float) $headers['x-imgtrans-total-ms'] : null,
        );
    }

    /**
     * @param string $path
     *
     * @return resource|false
     */
    private function newHandle($path)
    {
        $ch = curl_init($this->baseUrl . $path);
        if ($ch === false) {
            throw new ImgTransException('curl 初始化失败', 'E_CURL');
        }
        curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, $this->connectTimeout);
        curl_setopt($ch, CURLOPT_TIMEOUT, $this->timeout);

        return $ch;
    }

    /**
     * 把 `{"error":{"code","message"}}` 转成异常；非 JSON 响应退回 HTTP 状态码。
     *
     * @param int         $status
     * @param string      $raw
     * @param mixed       $data json_decode 的结果
     *
     * @return ImgTransException
     */
    private function buildError($status, $raw, $data)
    {
        if (is_array($data) && isset($data['error']['code'])) {
            $code = (string) $data['error']['code'];
            $message = isset($data['error']['message']) ? (string) $data['error']['message'] : $code;

            return new ImgTransException($message, $code, $status);
        }

        $snippet = trim(mb_substr((string) $raw, 0, 200, 'UTF-8'));

        return new ImgTransException('HTTP ' . $status . ': ' . $snippet, 'E_HTTP', $status);
    }

    /**
     * rename 在 Windows 上遇到已存在的目标文件可能失败，退回 copy + unlink。
     *
     * @param string $from
     * @param string $to
     *
     * @return void
     *
     * @throws ImgTransException
     */
    private function moveFile($from, $to)
    {
        if (@rename($from, $to)) {
            return;
        }
        if (@copy($from, $to)) {
            @unlink($from);

            return;
        }
        @unlink($from);
        throw new ImgTransException('写入目标文件失败: ' . $to, 'E_CLIENT');
    }

    /**
     * 上传时告诉服务端真实 MIME。不传的话服务端拿到的可能是
     * application/octet-stream，虽然能靠内容嗅探兜底，但没必要赌。
     *
     * @param string $path
     *
     * @return string
     */
    private function mimeOf($path)
    {
        if (class_exists('finfo')) {
            $finfo = new finfo(FILEINFO_MIME_TYPE);
            $mime = $finfo->file($path);
            if (is_string($mime) && $mime !== '') {
                return $mime;
            }
        }
        $ext = strtolower(pathinfo($path, PATHINFO_EXTENSION));
        $map = array(
            'jpg' => 'image/jpeg',
            'jpeg' => 'image/jpeg',
            'png' => 'image/png',
            'webp' => 'image/webp',
            'bmp' => 'image/bmp',
            'gif' => 'image/gif',
        );

        return isset($map[$ext]) ? $map[$ext] : 'application/octet-stream';
    }
}
