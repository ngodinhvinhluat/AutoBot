<#
  Build + deploy AutoBot len k3s, KHONG dung ACR (nap image thang vao containerd cua node qua SSH).
  Chay tu thu muc du an:
      .\deploy-k3s.ps1 -Tag 0.1.1
      .\deploy-k3s.ps1 -Tag 0.1.0 -SkipBuild      # image da build san trong WSL
      .\deploy-k3s.ps1 -Tag 0.1.1 -SkipImport     # chi apply manifest / doi secret
  Thong tin ha tang (context, node, host, registry...) doc tu deploy-k3s.local.psd1 va overlay k8s/local/
  (ca hai da .gitignore). Lan dau: chep deploy-k3s.local.example.psd1 va k8s/local.example/ roi dien gia tri.
  Tham so dong lenh (-Context, -Node, ...) ghi de gia tri trong file.
  Se hoi: mat khau SSH (scp + ssh) va mat khau sudo tren node.
  Bo hoi mat khau SSH: ssh-copy-id <user>@<node> (chay 1 lan trong WSL hoac Git Bash).
  Chi tiet: DEPLOY_K3S.md
#>
param(
    [string]$Tag = "0.1.0",
    [switch]$SkipBuild,
    [switch]$SkipImport,
    [switch]$SkipVerify,
    [string]$Context,
    [string]$Namespace,
    [string]$Node,
    [string]$HostName,
    [string]$Image,
    [string]$AcrSecretFrom,
    [string]$Overlay = "k8s/local",
    [string]$Distro = "Ubuntu",
    # Node k3s la VM kvm64 (khong AVX): VieNeu cham ~14x thoi gian thuc -> dung TTS tren cloud. "" = giu nguyen settings.json
    [string]$TtsEngine = "edge_tts"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$cfgFile = Join-Path $PSScriptRoot "deploy-k3s.local.psd1"
$cfg = if (Test-Path $cfgFile) { Import-PowerShellDataFile $cfgFile } else { @{} }
foreach ($k in "Context", "Namespace", "Node", "HostName", "Image", "AcrSecretFrom") {
    if (-not (Get-Variable $k -ValueOnly) -and $cfg[$k]) { Set-Variable $k $cfg[$k] }
}
foreach ($k in "Context", "Namespace", "HostName", "Image") {
    if (-not (Get-Variable $k -ValueOnly)) { throw "Thieu ${k}: dien vao deploy-k3s.local.psd1 (mau: deploy-k3s.local.example.psd1) hoac truyen -$k" }
}
if (-not $SkipImport -and -not $Node) { throw "Thieu Node (user@ip) de import image" }
if (-not (Test-Path "$Overlay/kustomization.yaml")) { throw "Chua co overlay $Overlay/: chep k8s/local.example/ thanh $Overlay/ roi dien gia tri" }
$Image = "${Image}:$Tag"
$Tar = Join-Path $env:TEMP "autobot-$Tag.tar"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what that bai (exit $LASTEXITCODE)" } }
function Kube { & kubectl --context $Context @args; Check "kubectl $($args -join ' ')" }

# ---------- 1. Build image trong WSL ----------
if (-not $SkipBuild) {
    Step "Build $Image (Docker trong WSL $Distro)"
    $proj = (wsl -d $Distro -- wslpath -a ($PSScriptRoot -replace '\\', '/')).Trim()
    wsl -d $Distro -- bash -lc "cd '$proj' && sed -i 's/\r$//' docker-entrypoint.sh && docker build -t '$Image' ."
    Check "docker build"
}

# ---------- 2. Nap image vao node ----------
if (-not $SkipImport) {
    Step "Xuat image ra $Tar"
    $tarWsl = (wsl -d $Distro -- wslpath -a ($Tar -replace '\\', '/')).Trim()
    wsl -d $Distro -- docker save $Image -o $tarWsl
    Check "docker save"
    "{0:N0} MB" -f ((Get-Item $Tar).Length / 1MB)

    Step "Chep len node $Node (nhap mat khau SSH)"
    scp $Tar "${Node}:/tmp/autobot-$Tag.tar"
    Check "scp"

    Step "Import vao containerd k3s (nhap mat khau SSH, roi mat khau sudo)"
    ssh -t $Node "sudo k3s ctr -n k8s.io images import /tmp/autobot-$Tag.tar && rm -f /tmp/autobot-$Tag.tar && sudo k3s crictl images | grep autobot"
    Check "ssh import"
    Remove-Item $Tar -ErrorAction SilentlyContinue
}

# ---------- 3. Namespace, secret, manifest ----------
Step "Namespace + secret tren $Context/$Namespace"
kubectl --context $Context create namespace $Namespace --dry-run=client -o yaml | kubectl --context $Context apply -f -
Check "namespace $Namespace"
$acr = kubectl --context $Context -n $Namespace get secret acr-secret --ignore-not-found -o name
if (-not $acr -and $AcrSecretFrom) {
    $src = kubectl --context $Context -n $AcrSecretFrom get secret acr-secret -o json | ConvertFrom-Json
    if ($src) {
        $copy = @{ apiVersion = "v1"; kind = "Secret"; type = $src.type
                   metadata = @{ name = "acr-secret"; namespace = $Namespace }; data = $src.data }
        $copy | ConvertTo-Json -Depth 5 | kubectl --context $Context apply -f -
        Check "copy acr-secret"
    }
}
if (Test-Path .env) {
    kubectl --context $Context -n $Namespace create secret generic autobot-env --from-env-file=.env --dry-run=client -o yaml |
        kubectl --context $Context apply -f -
    Check "secret autobot-env"
} else {
    Write-Warning "Khong co .env: giu nguyen secret autobot-env dang co tren cum"
}

Step "Dat tag $Tag trong $Overlay/kustomization.yaml va apply"
$kz = "$Overlay/kustomization.yaml"
$text = [IO.File]::ReadAllText((Resolve-Path $kz))
$text = $text -replace 'newTag:\s*"[^"]*"', "newTag: `"$Tag`""
[IO.File]::WriteAllText((Resolve-Path $kz), $text, (New-Object Text.UTF8Encoding $false))
Kube apply -k $Overlay
# Pod dang ErrImagePull/BackOff thi tao lai ngay, khong cho het thoi gian backoff
$bad = kubectl --context $Context -n $Namespace get pod -l app=autobot -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}'
if ($bad -match 'ImagePull|ErrImage') { Kube -n $Namespace delete pod -l app=autobot --wait=false }
Kube -n $Namespace rollout status deploy/autobot --timeout=10m
Kube -n $Namespace get pod -l app=autobot -o wide

if ($TtsEngine) {
    Step "Dat tts_engine=$TtsEngine (luu vao /data/settings.json tren PVC)"
    $body = @{ tts_engine = $TtsEngine } | ConvertTo-Json -Compress
    $r = Invoke-RestMethod -Method Post "http://$HostName/api/settings" -ContentType "application/json" -Body $body -TimeoutSec 20
    "tts_engine = $($r.values.tts_engine), tts_output = $($r.values.tts_output)"
}

# ---------- 4. Kiem tra ----------
if (-not $SkipVerify) {
    Step "Kiem tra http://$HostName"
    foreach ($p in "/", "/api/state", "/static/dashboard.js") {
        $r = Invoke-WebRequest "http://$HostName$p" -UseBasicParsing -TimeoutSec 20
        "{0,-22} {1}" -f $p, $r.StatusCode
    }
    Step "Kiem tra WebSocket qua Traefik (giu 70s)"
    $py = @"
import asyncio, time, websockets
async def m():
    async with websockets.connect('ws://$HostName/ws/audio?format=pcm&vad=auto&dev=deploy-check', open_timeout=20) as ws:
        t = time.time()
        await asyncio.sleep(70)
        await (await ws.ping())
        print('WS OK, giu ket noi %.0fs' % (time.time() - t))
asyncio.run(m())
"@
    python -I -c $py
    Check "WebSocket check"
}
Write-Host "`nXong: http://$HostName  (ESP32: SERVER_MODE SERVER_K3S)" -ForegroundColor Green
