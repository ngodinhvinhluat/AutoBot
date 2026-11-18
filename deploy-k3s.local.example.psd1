# Mau cau hinh ha tang cho deploy-k3s.ps1.
# Chep thanh deploy-k3s.local.psd1 (da .gitignore) roi dien gia tri that.
@{
    Context       = "my-k3s"                          # kubectl context
    Namespace     = "autobot"                         # trung namespace trong k8s/local/kustomization.yaml
    Node          = "user@NODE_IP"                    # SSH vao node k3s de import image
    HostName      = "autobot.NODE_IP.nip.io"          # host cua Ingress
    Image         = "registry.example.com/autobot"    # trung newName trong k8s/local/kustomization.yaml
    AcrSecretFrom = ""                                # namespace co san secret acr-secret de chep sang ("" = bo qua)
}
