FROM python:3.12-slim
WORKDIR /app
COPY app.py hls.py metrics.py diagnostics.py throttle.py preload.py bili.py mp4.py system_stats.py settings.py nas_config.py index.html ui.css ui.js settings-app.js ./
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
RUN mkdir -p /data /host/proc && touch /host/proc/stat /host/proc/meminfo && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
