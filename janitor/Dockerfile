FROM alpine:3.20
# TDR §4: Alpine-based Janitor — GFS Backup Shell + 30-day Media Prune
RUN apk add --no-cache bash coreutils findutils
WORKDIR /app
COPY janitor.sh /app/janitor.sh
RUN chmod +x /app/janitor.sh
CMD ["/bin/sh", "/app/janitor.sh"]
