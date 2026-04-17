
# SPEAKR的自定义讯飞asr_endpoint

目前实现了讯飞极速录音转写大模型API对接，后续可以对接qwen的在线或本地api。

下面这套是按 Speakr 的 asr_endpoint 接口约定 写的完整适配器：Speakr 会把音频文件 POST 到 /asr，字段名是 audio_file，并带上 language、diarize、enable_diarization、min_speakers、max_speakers 等参数；返回体里至少要有 text、language、segments。Speakr 官方示例也说明了，只要设置 ASR_BASE_URL，它就会走这个自定义 ASR endpoint 模式。

参考文档：

https://raw.githubusercontent.com/murtaza-nasir/speakr/master/src/services/transcription/connectors/asr_endpoint.py

https://www.xfyun.cn/doc/asr/speedTranscription/API.html#%E6%8E%A5%E5%8F%A3%E8%AF%B4%E6%98%8E

这版代码覆盖了几块：

- HMAC-SHA256 鉴权
- 小文件上传 `POST /file/upload`
- 大文件分块上传 `init -> upload -> complete`
- 创建任务 `POST /v2/ost/pro_create`
- 轮询任务 `POST /v2/ost/query`
- 转成 Speakr 可消费的 JSON 格式返回。讯飞这套“极速录音转写”官方文档也确实就是这五步流程，且文档说明小文件阈值是 30MB、总文件上限 500MB、最长 5 小时。

直接跑本地开发可以这样：

```
pip install -r requirements.txt
export XF_APPID=appid
export XF_API_KEY=apikey
export XF_API_SECRET=apisecret
uvicorn xfyun_speakr_adapter:app --host 0.0.0.0 --port 9000
```

Docker 跑法：

```
docker build -t xfyun-speakr-adapter .
docker run --rm -p 9000:9000 \
  -e XF_APPID=appid \
  -e XF_API_KEY=apikey \
  -e XF_API_SECRET=apisecret \
  xfyun-speakr-adapter
```

然后在 Speakr 里这样配：

```
TRANSCRIPTION_CONNECTOR=asr_endpoint
ASR_BASE_URL=http://<localhost>:9000
ASR_DIARIZE=false
```

如果是和 Speakr 放在同一个 docker-compose 里，`ASR_BASE_URL` 用容器名加内部端口更稳。Speakr 的配置示例里也是这么写的。

还需要知道几个实现细节。

第一，讯飞鉴权必须带 `host`、`date`、`digest`、`authorization` 这些请求头，`date` 要用 RFC1123 的 GMT/UTC 时间，而且服务端只允许大约 300 秒时钟偏差。代码里已经按文档规则实现了签名串：`host + date + request-line + digest`，再用 `APISecret` 做 HMAC-SHA256。

第二，创建任务时，这版默认按讯飞文档设置 `domain=pro_ost_ed`、`accent=mandarin`，并把 `language` 规范化成常用值；文档对这套接口给出的主参数就是这些。

第三，说话人分离这版也接上了：如果 Speakr 传 `diarize=true`，会映射到讯飞的 `vspp_on=1`；如果 `min_speakers == max_speakers`，就把它映射成 `speaker_num`，否则走盲分。讯飞文档也写了 `speaker_num=0` 表示盲分。另一个很关键的限制是：**mp3 目前不支持角色分离**，所以代码里对 mp3 会自动关闭 diarization。

第四，返回结果里做了 Speakr 友好的转换：

- `text`：尽量拼成整段文本
- `segments`：每段包含 `speaker`、`text`、`start`、`end`
- `language`：回传 Speakr 传入值或默认 `zh`
   这和 Speakr 的 `asr_endpoint` 解析逻辑是对齐的。

