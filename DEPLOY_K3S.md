# Đóng gói và triển khai AutoBot lên k3s

Tài liệu cho người vận hành (hoặc session Claude Code sau) cần build image AutoBot và chạy nó trên cụm k3s nội bộ.

## 1. Kiến trúc

```
ESP32 ──ws://autobot.<NODE_IP>.nip.io/ws/audio──┐
Trình duyệt ──http://autobot.<NODE_IP>.nip.io/──┤
                                                     ▼
                         Traefik (k3s, cổng 80 trên node <NODE_IP>)
                                                     │  Ingress "autobot"
                                                     ▼
                   Service autobot:80 ──> Pod autobot:8000 (1 bản duy nhất)
                                                     │
                            PVC autobot-data (local-path, 5Gi) gắn vào /data
                              ├─ settings.json      (sửa trên Dashboard)
                              ├─ voice_session.log
                              ├─ recordings/
                              └─ cache/             (model VieNeu, HuggingFace)
```

| Thành phần | Giá trị |
|---|---|
| Cụm | context `<context>`, node `<NODE_IP>` |
| Namespace | `<namespace>` |
| Image | `<registry>/autobot:<tag>` (pull bằng `acr-secret` nếu dùng registry riêng) |
| Host | `autobot.<NODE_IP>.nip.io` |
| Bí mật | Secret `autobot-env` tạo từ `.env` (GEMINI_API_KEY, DXAI_*), **không** nằm trong image hay git |

Vì sao chỉ chạy 1 bản (`replicas: 1`, `strategy: Recreate`):
- Phiên hội thoại, lịch sử theo thiết bị và model TTS nằm trong RAM của tiến trình.
- PVC `local-path` là ReadWriteOnce, chỉ gắn được vào 1 pod.
- Khi cập nhật, pod cũ dừng hẳn rồi pod mới mới chạy. ESP32 mất kết nối vài giây rồi tự nối lại.

## 2. Các file

| File | Vai trò |
|---|---|
| `Dockerfile` | `python:3.12-slim` + flac, libsndfile1, libgomp1. Chạy user 10001. `AUTOBOT_DATA_DIR=/data`, `HF_HOME` và `VIENEU_LOCAL_DIR` trỏ vào `/data/cache` |
| `docker-entrypoint.sh` | Tạo `/data/recordings`, chép `settings.default.json` sang `/data/settings.json` khi volume còn trống |
| `.dockerignore` | Loại `.env`, log, ghi âm, firmware, file wav, tài liệu |
| `k8s/base/` | namespace, pvc, deployment, service, ingress + kustomization. Dùng chung, **không** chứa IP, registry hay namespace thật |
| `k8s/local.example/` | Mẫu overlay: namespace thật, tên image (`newName`) và **tag** (`newTag`), host của Ingress |
| `k8s/local/` | Overlay thật, **đã .gitignore**. Chép từ `local.example/` rồi điền giá trị |
| `deploy-k3s.local.example.psd1` | Mẫu cấu hình hạ tầng cho script: context, namespace, node SSH, host, image, namespace có sẵn `acr-secret` |
| `deploy-k3s.local.psd1` | Cấu hình thật, **đã .gitignore** |

Thông tin hạ tầng (IP node, user SSH, registry, namespace) chỉ nằm trong 2 file local trên và `esp32_full_duplex/secrets.h`, không lên Git. Máy mới clone repo về thì làm một lần:

```powershell
Copy-Item -Recurse k8s/local.example k8s/local
Copy-Item deploy-k3s.local.example.psd1 deploy-k3s.local.psd1
# Sửa giá trị trong 2 chỗ trên cho khớp cụm. Namespace và Image phải trùng nhau ở cả 2 file.
```

Thay đổi trong code để chạy được trong container:
- `autobot/core/paths.py`: đọc `AUTOBOT_DATA_DIR`. Không đặt biến này thì vẫn dùng thư mục gốc dự án như trước.
- `autobot/tts/vieneu_engine.py`: đọc `VIENEU_LOCAL_DIR` cho thư mục chép model ONNX int8.

## 3. Chạy bằng một lệnh: `deploy-k3s.ps1`

Script [deploy-k3s.ps1](deploy-k3s.ps1) chạy toàn bộ mục 4 bên dưới: build trong WSL, xuất tar, scp lên node, `k3s ctr import`, tạo namespace và secret, đặt tag trong `k8s/local/kustomization.yaml`, `apply -k k8s/local`, chờ rollout, rồi kiểm tra HTTP và WebSocket (giữ 70 giây).

```powershell
cd C:\Work\App\AutoBot
powershell -ExecutionPolicy Bypass -File .\deploy-k3s.ps1 -Tag 0.1.1              # bản mới, build lại
powershell -ExecutionPolicy Bypass -File .\deploy-k3s.ps1 -Tag 0.1.0 -SkipBuild   # image đã build sẵn
powershell -ExecutionPolicy Bypass -File .\deploy-k3s.ps1 -Tag 0.1.0 -SkipBuild -SkipImport   # chỉ đổi secret/manifest
```

Script sẽ hỏi mật khẩu SSH (2 lần, cho scp và ssh) và mật khẩu sudo trên node. Node chỉ cho đăng nhập bằng mật khẩu, nên agent không tự chạy bước import được. Muốn bỏ bước hỏi mật khẩu SSH thì chạy một lần `ssh-copy-id <user>@<NODE_IP>` trong WSL hoặc Git Bash.

## 4. Triển khai từng bước (thủ công)

Chạy từ `C:\Work\App\AutoBot`. Docker dùng engine trong WSL Ubuntu.

### Bước 1: Build image

```powershell
$TAG = "0.1.0"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Work/App/AutoBot && docker build -t <registry>/autobot:$TAG ."
```

Thử nhanh trên máy trước khi đẩy lên cụm:

```powershell
wsl -d Ubuntu -- bash -lc "docker run --rm -d --name autobot-test -p 18000:8000 --env-file /mnt/c/Work/App/AutoBot/.env <registry>/autobot:$TAG"
Invoke-WebRequest http://localhost:18000/api/state -UseBasicParsing | Select-Object StatusCode
wsl -d Ubuntu -- docker rm -f autobot-test
```

### Bước 2: Đưa image lên node

**Cách A, không dùng ACR (đang dùng):** nạp thẳng vào containerd của k3s. Cần SSH và sudo trên node.

```powershell
wsl -d Ubuntu -- docker save <registry>/autobot:$TAG -o /mnt/c/Temp/autobot-$TAG.tar   # khoảng 383 MB
scp C:\Temp\autobot-$TAG.tar <user>@<NODE_IP>:/tmp/
ssh -t <user>@<NODE_IP> "sudo k3s ctr images import /tmp/autobot-$TAG.tar && sudo k3s ctr images ls -q | grep autobot && rm /tmp/autobot-$TAG.tar"
```

Image giữ tên `<registry>/autobot:<tag>`. Với `imagePullPolicy: IfNotPresent`, kubelet thấy image có sẵn trên node nên không pull. Cụm chỉ có 1 node nên cách này đủ dùng. Sau này muốn chuyển sang ACR thì chỉ cần push đúng tên đó, không phải sửa manifest.

**Cách B, đẩy lên ACR:**

```powershell
az acr login --name <acr>    # hoặc: docker login <registry> (tài khoản ACR của nhóm)
wsl -d Ubuntu -- docker push <registry>/autobot:$TAG
```

Lưu ý: nếu `az` chạy trên Windows còn docker chạy trong WSL thì phải đăng nhập **bên trong WSL** (`wsl -d Ubuntu -- docker login <registry>`).

### Bước 3: Tạo namespace, quyền pull image, Secret

```powershell
kubectl --context <context> create namespace <namespace>

# Chép acr-secret từ namespace có sẵn sang <namespace>
kubectl --context <context> -n <ns-co-acr-secret> get secret acr-secret -o json |
  ConvertFrom-Json | ForEach-Object {
    $_.metadata = @{ name = 'acr-secret'; namespace = '<namespace>' }; $_ } |
  ConvertTo-Json -Depth 10 | kubectl --context <context> apply -f -

# Secret biến môi trường lấy từ .env (dòng bắt đầu bằng # được bỏ qua)
kubectl --context <context> -n <namespace> create secret generic autobot-env --from-env-file=.env
```

Đổi key sau này:

```powershell
kubectl --context <context> -n <namespace> create secret generic autobot-env --from-env-file=.env --dry-run=client -o yaml |
  kubectl --context <context> apply -f -
kubectl --context <context> -n <namespace> rollout restart deploy/autobot
```

### Bước 4: Áp manifest

Sửa `newTag` trong `k8s/local/kustomization.yaml` cho trùng `$TAG`, rồi:

```powershell
kubectl --context <context> apply -k k8s/local
kubectl --context <context> -n <namespace> rollout status deploy/autobot --timeout=10m
```

### Bước 5: Kiểm tra

```powershell
$u = 'http://autobot.<NODE_IP>.nip.io'
Invoke-WebRequest "$u/api/state" -UseBasicParsing | Select-Object StatusCode
python -c "import asyncio,websockets
async def m():
    async with websockets.connect('ws://autobot.<NODE_IP>.nip.io/ws/audio?format=pcm&vad=auto', open_timeout=15) as ws:
        await asyncio.sleep(90); await ws.ping(); print('WS OK sau 90s')
asyncio.run(m())"
```

Giữ WebSocket quá 60 giây để chắc chắn Traefik không cắt kết nối rảnh.

## 5. ESP32

Trong `esp32_full_duplex/esp32_full_duplex.ino` đặt:

```cpp
#define SERVER_MODE  SERVER_K3S
```

ESP32 kết nối `ws://autobot.<NODE_IP>.nip.io:80/ws/audio...`. Điều kiện:
- WiFi của ESP32 phải đi tới được <NODE_IP> (ESP32 ở dải mạng khác thì cần định tuyến giữa 2 dải).
- DNS của WiFi phải phân giải được `nip.io`. Nếu DNS nội bộ chặn, thêm bản ghi `autobot.<NODE_IP>.nip.io → <NODE_IP>` vào DNS nội bộ, hoặc đổi Ingress sang tên miền nội bộ khác.
- Phải dùng **tên miền**, không dùng IP: Traefik chọn app theo header Host.

## 6. Vận hành

| Việc | Lệnh (thêm `--context <context> -n <namespace>`) |
|---|---|
| Xem pod | `kubectl get pod -o wide` |
| Log tiến trình | `kubectl logs deploy/autobot -f` |
| Log hội thoại | `kubectl exec deploy/autobot -- tail -n 100 /data/voice_session.log` |
| Tải ghi âm về máy | `kubectl cp <pod>:/data/recordings ./recordings-k3s` |
| Khởi động lại | `kubectl rollout restart deploy/autobot` |
| Lên bản mới | Build và push tag mới, sửa `newTag` trong `k8s/local/`, chạy `kubectl apply -k k8s/local` |
| Quay lại bản trước | `kubectl rollout undo deploy/autobot` |
| Tạm tắt (giữ dữ liệu) | `kubectl scale deploy/autobot --replicas=0` |
| Gỡ hẳn | `kubectl delete -k k8s/local`. Lệnh này xoá cả namespace và **PVC, mất settings, log, ghi âm** |

## 7. Lưu ý

- **Giọng đọc VieNeu**: `settings.json` đóng trong image đang bật giọng đọc (int8). Lần đầu pod tải model vào `/data/cache` (đo được 444 MB), `startupProbe` cho tối đa 10 phút. Các lần sau lấy model từ cache, mất khoảng 18-25 giây là server sẵn sàng.
- **RAM**: đo khi chạy thử là khoảng 815 MiB với int8. Đang đặt `requests.memory=1Gi`, `limits.memory=3Gi` để còn dư khi đổi sang fp32. Bị OOMKilled thì nâng `limits.memory`.
- **Image**: khoảng 1,6 GB, phần lớn là `vieneu` kéo theo `gradio`, `onnxruntime`, `librosa`, `numba`. Lần pull đầu trên node sẽ hơi lâu.
- **CPU**: VieNeu chạy ONNX trên CPU, giới hạn 2 core. Node dùng chung với các app khác thì đừng nâng quá cao.
- **settings.json**: lần đầu lấy từ bản đóng trong image. Sau đó file trên PVC là bản chính, build image mới **không** ghi đè. Muốn quay về mặc định thì xoá `/data/settings.json` rồi khởi động lại pod.
- **`.env` trong image**: không có. Code vẫn gọi `load_dotenv`, nhưng không thấy file nên dùng biến môi trường từ Secret.
- **ArgoCD**: AutoBot hiện áp tay bằng `kubectl apply -k`. Muốn chuyển sang ArgoCD thì đưa `k8s/base` và overlay vào repo gitops (riêng tư) và tạo Application trỏ tới nó.
- **Không có registry**: có thể `docker save` image, chép lên node rồi `sudo k3s ctr images import autobot.tar`, đặt `imagePullPolicy: Never`. Cách này cần SSH vào node.
