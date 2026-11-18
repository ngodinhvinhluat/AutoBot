# Hướng dẫn gắn thêm app vào domain ngrok (chia theo đường dẫn)

Tài liệu dành cho session sau (người hoặc Claude Code) khi cần đưa thêm một app local ra Internet qua domain ngrok sẵn có.

> **Không có subdomain thật.** Gói ngrok free chỉ có **1 domain tĩnh**. Không tạo được `app.unexpired-dugout-bush.ngrok-free.dev` và cũng không dùng được port khác 443 (`domain:4500` không chạy).
> Mỗi app được gắn vào **một tiền tố đường dẫn**: `https://unexpired-dugout-bush.ngrok-free.dev/<prefix>/`.

---

## 1. Hiện trạng (2026-11-17)

| Thành phần | Giá trị |
|---|---|
| Domain | `https://unexpired-dugout-bush.ngrok-free.dev` |
| ngrok | `C:\Tools\ngrok\ngrok.exe` (v3.39.13, không có trong PATH) |
| Authtoken | `%LOCALAPPDATA%\ngrok\ngrok.yml`, đã cấu hình, **không chép token vào file khác** |
| Cấu hình chia đường dẫn | `C:\Work\App\AutoBot\ngrok.yml` |
| Dashboard theo dõi request | http://127.0.0.1:4040 |

| Đường dẫn | Port local | App |
|---|---|---|
| `/` | 8000 | AutoBot chính (ESP32 gọi `wss://<domain>/ws/audio`) |
| `/b/` | 4500 | AutoBot instance 2 (`$env:PORT=4500; python server.py`) |

Lệnh chạy ngrok, từ thư mục `C:\Work\App\AutoBot`:

```powershell
C:\Tools\ngrok\ngrok.exe start --all --config "$env:LOCALAPPDATA\ngrok\ngrok.yml" --config ngrok.yml
```

---

## 2. Cách hoạt động

```
Internet ──> https://domain/<prefix>/xyz
               │  traffic_policy (endpoint "public")
               │   1. /<prefix>     -> redirect 301 sang /<prefix>/
               │   2. /<prefix>/... -> url-rewrite bỏ tiền tố -> forward-internal tới https://<name>.internal
               │   3. còn lại       -> forward-internal tới https://site-a.internal (AutoBot :8000)
               ▼
           internal endpoint ──> http://localhost:<port>/xyz
```

- ngrok **bỏ tiền tố** trước khi chuyển vào, nên app nhận `/xyz` như khi chạy bình thường.
- ngrok **không sửa nội dung HTML/JS** mà app trả về. Nếu trang dùng đường dẫn tuyệt đối (`/static/a.js`, `fetch('/api/x')`), trình duyệt sẽ gọi thiếu tiền tố và request rơi vào AutoBot (404). Bước 3 bên dưới xử lý chuyện này.

---

## 3. Các bước thêm app mới

Ví dụ: thêm app ở **port 5000**, tiền tố **`/c/`**, tên endpoint **`site-c`**.

### Bước 1: Chọn tiền tố và kiểm tra app đang chạy

- Tiền tố ngắn, **không trùng** tiền tố đã có (`/b/`) và **không trùng route của AutoBot** (`/static`, `/api`, `/settings`, `/action`, `/ws`).
- Kiểm tra port:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5000 | Select-Object LocalPort,OwningProcess
Invoke-WebRequest http://localhost:5000/ -UseBasicParsing | Select-Object StatusCode
```

### Bước 2: Kiểm tra app dùng đường dẫn tuyệt đối hay tương đối

Tìm trong HTML/JS/template của app:

```powershell
# Thay <thư mục web của app> cho đúng
Select-String -Path <thư mục web của app>\* -Pattern '(href|src|action)="/[^/]|fetch\(\s*[''"`]/|new WebSocket\(' -List
```

Không có kết quả nghĩa là app dùng đường dẫn tương đối, nhảy sang **Bước 4**.

### Bước 3: Cho app chạy được dưới tiền tố (chọn một cách)

**Cách A, sửa sang đường dẫn tương đối** (đã làm với AutoBot, đơn giản nhất nếu sửa được code):
`href="/static/x.css"` thành `href="static/x.css"`, `fetch('/api/x')` thành `fetch('api/x')`.
Ở `/` vẫn chạy như cũ, ở `/c/` trình duyệt tự gọi `/c/static/x.css`.
Riêng WebSocket trong JS thì tạo URL từ trang hiện tại:
`new WebSocket(location.href.replace(/^http/, 'ws').replace(/[^/]*$/, '') + 'ws/...')`.

**Cách B, dùng cấu hình base path của framework:**

| Framework | Cấu hình |
|---|---|
| Angular | `ng build --base-href /c/` (hoặc `ng serve --serve-path /c/` kèm **bỏ** `url-rewrite`, xem ghi chú dưới) |
| Vite (React/Vue) | `base: '/c/'` trong `vite.config` |
| Create React App | `"homepage": "/c/"` trong `package.json` |
| Next.js | `basePath: '/c'` trong `next.config.js` (khi đó **bỏ** `url-rewrite`) |
| FastAPI | `FastAPI(root_path="/c")`, link trong HTML vẫn phải tương đối hoặc dùng `request.url_for` |
| Flask | dùng `url_for()` trong template, kết hợp middleware `SCRIPT_NAME=/c` |
| ASP.NET Core | `app.UsePathBase("/c")` (khi đó **bỏ** `url-rewrite`) |

> Ghi chú: nếu app **tự xử lý** tiền tố (Next.js `basePath`, `UsePathBase`, `ng serve --serve-path`) thì app phải nhận nguyên `/c/...`. Khi đó **xoá action `url-rewrite`** trong rule của app đó.

**Cách C, không sửa được app:** thêm luật theo header `Referer` (mục 5). Cách này là mẹo, có giới hạn.

### Bước 4: Sửa `ngrok.yml`

Thêm **internal endpoint** vào danh sách `endpoints:`:

```yaml
  - name: site-c
    url: https://site-c.internal
    upstream:
      url: 5000
```

Trong `traffic_policy.on_http_request` của endpoint `public`, chèn 2 rule **TRƯỚC rule cuối cùng** (rule không có `expressions` là catch-all, phải luôn nằm cuối):

```yaml
        - expressions:
            - "req.url.path == '/c'"
          actions:
            - type: redirect
              config:
                to: "/c/"
                status_code: 301
        - expressions:
            - "req.url.path.startsWith('/c/')"
          actions:
            - type: url-rewrite          # xoá action này nếu app tự xử lý tiền tố
              config:
                from: "/c/(.*)"
                to: "/$1"
            - type: forward-internal
              config:
                url: https://site-c.internal
```

Không xoá `upstream` của endpoint `public`. ngrok bắt buộc phải có nó, dù request thực tế đã được `forward-internal` chuyển đi.

### Bước 5: Khởi động lại ngrok

Mỗi domain chỉ có **một** agent giữ được, nên phải tắt agent cũ trước:

```powershell
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Set-Location C:\Work\App\AutoBot
C:\Tools\ngrok\ngrok.exe start --all --config "$env:LOCALAPPDATA\ngrok\ngrok.yml" --config ngrok.yml
```

Log đúng phải có các dòng `started tunnel ... name=site-c addr=http://localhost:5000 url=https://site-c.internal` và `name=public ... url=https://unexpired-dugout-bush.ngrok-free.dev`.

### Bước 6: Kiểm tra

```powershell
$b='https://unexpired-dugout-bush.ngrok-free.dev'; $h=@{'ngrok-skip-browser-warning'='1'}
foreach($p in '/','/b/','/c','/c/','/c/<một file tĩnh>','/c/<một API>'){
  try { $r=Invoke-WebRequest "$b$p" -UseBasicParsing -Headers $h; "{0,-30} {1} len={2}" -f $p,$r.StatusCode,$r.RawContentLength }
  catch { "{0,-30} ERR {1}" -f $p,$_.Exception.Response.StatusCode.value__ }
}
```

- `/` và `/b/` vẫn phải trả 200, để chắc chắn không làm hỏng site cũ.
- Mở `https://<domain>/c/` bằng trình duyệt, bấm F12 → tab Network, và **không được có request nào thiếu `/c/`** mà trả 404.
- WebSocket (nếu app có):

```powershell
python -c "import asyncio,websockets
async def m():
    async with websockets.connect('wss://unexpired-dugout-bush.ngrok-free.dev/c/<ws path>', open_timeout=15):
        print('WS OK')
asyncio.run(m())"
```

---

## 4. Lỗi thường gặp

| Lỗi | Nguyên nhân / cách xử lý |
|---|---|
| `ERR_NGROK_3200` endpoint offline | Agent ngrok không chạy, xem Bước 5 |
| `ERR_NGROK_334` endpoint already online | Đang có agent khác giữ domain (terminal khác, máy khác), tắt agent đó đi |
| `ERR_NGROK_105` authtoken không hợp lệ | Đã dán **Domain ID** (`rd_...`, ~30 ký tự) thay cho authtoken. Lấy đúng ở dashboard.ngrok.com → *Your Authtoken* rồi chạy `ngrok.exe config add-authtoken <token>` |
| `Endpoint 'public' must specify 'url'` | Thiếu `upstream.url` ở endpoint `public` |
| Defender xoá/chặn `ngrok.exe` | Defender xếp ngrok vào nhóm PUA. Vào Windows Security → Protection history → Restore/Allow. **Không tự thêm exclusion** khi chưa được cho phép |
| Trang `/c/` trắng hoặc mất CSS | App dùng đường dẫn tuyệt đối, xem Bước 3 |
| `/c/` lại hiện AutoBot | Rule `/c/` nằm **sau** rule catch-all, hoặc chưa khởi động lại ngrok |
| Trình duyệt hiện trang cảnh báo của ngrok | Đây là trang chặn của gói free, chỉ hiện với trình duyệt. Bấm *Visit Site*. Client không phải trình duyệt (ESP32, script) không bị ảnh hưởng. API gọi từ code có thể gửi header `ngrok-skip-browser-warning: 1` |
| `winget install Ngrok.Ngrok` | Bản trên winget quá cũ (3.3.1), không có `--url`. Tải zip từ `https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip` |
| Gói free báo vượt giới hạn endpoint | Mỗi app tốn 1 internal endpoint. Gỡ bớt site không dùng hoặc nâng gói |

---

## 5. Mẹo Referer (chỉ dùng khi không sửa được app)

Khi trang `/c/` tải file con, trình duyệt gửi `Referer: https://domain/c/...`. Thêm rule sau ngay **trước** rule catch-all để request thiếu tiền tố vẫn về đúng app:

```yaml
        - expressions:
            - "'referer' in req.headers && req.headers['referer'][0].contains('/c/')"
          actions:
            - type: forward-internal
              config:
                url: https://site-c.internal
```

Giới hạn:
- **Chưa kiểm tra thực tế** trên domain này. Thử và đối chiếu trên http://127.0.0.1:4040 trước khi dựa vào nó.
- Không áp dụng cho WebSocket từ trình duyệt (không gửi Referer) và cho link mở tab mới.
- Nếu app mới có route trùng AutoBot (vd. `/api/state`) thì chỉ phân biệt được nhờ Referer. Gọi trực tiếp không có Referer sẽ vào AutoBot.

---

## 6. Lưu ý riêng cho AutoBot

- Các instance AutoBot dùng **chung** `settings.json`, `voice_session.log`, `recordings/`. Đổi cài đặt ở một dashboard là ảnh hưởng instance kia.
- ESP32 (`esp32_full_duplex.ino`, `SERVER_MODE SERVER_NGROK`) gọi site `/`. Muốn ESP32 dùng site khác thì đổi `ws_path` thành `"/<prefix>/ws/audio?format=pcm&vad=auto"`.
- Đổi port AutoBot: `$env:PORT=<port>; python server.py`, mặc định là 8000.
