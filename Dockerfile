# 云梭 CloudShuttle · 控制台镜像
FROM python:3.12-alpine
# 换国内 apk 源 + 装 docker 客户端（控制台需要调用 docker exec 指挥 rclone 容器）
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apk/repositories \
 && apk add --no-cache docker-cli ca-certificates
WORKDIR /app
CMD ["python3", "-u", "download-console.py"]
