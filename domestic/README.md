# Prompt Nest · 国内服务器部署

部署包包含中文收藏页及兼容旧数据的服务端。页面无需加载第三方字体、图片或账号服务。

## 上线

在已安装 Docker 与 Compose 的 Linux 服务器上解压整个包，保留 `dist/index.html` 与 `domestic/` 的相对位置。在包的根目录执行：

```sh
docker compose -f domestic/compose.yaml up -d --build
```

服务监听服务器本机的 8080 端口。为自己的域名配置 HTTPS，在已有 Nginx HTTPS 站点的 server 块中使用以下配置。客户端请求上限包含媒体和表单开销。

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 26m;
    proxy_read_timeout 120s;
}
```

应用默认仅通过 HTTPS 发送会话 Cookie。本机 HTTP 试用可执行 `COOKIE_SECURE=0 docker compose -f domestic/compose.yaml up -d --build` 并打开 http://localhost:8080 。生产环境使用默认值。仅在请求全部经过上述可信反向代理时设置 `TRUST_PROXY_IP=1`，以便分别按客户端地址限流。基础镜像和 Python 包须在构建机器上可获取，也可预先构建镜像后迁移。

## 免登录收藏室

打开首页即可使用。新收藏及附件保存在当前浏览器的 IndexedDB 中。使用原网址和原浏览器可继续编辑；清除网站数据后，本地收藏也会清除。独立 HTML 页面可从 `dist/index.html` 打开，建议正式使用时固定一个 HTTPS 域名。

支持关键词搜索、图片和 MP4 预览与原文件下载、重命名、删除与单步撤回。每条可附一个最大 25 MiB 的文件。升级前已有的服务器数据保存在原数据卷，仍有效的旧会话可继续访问对应的原收藏。备份服务器数据卷不会备份浏览器内的收藏。
