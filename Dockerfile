FROM python:3.13-alpine
RUN apk add --no-cache ffmpeg
COPY streamer.py /app/streamer.py
EXPOSE 8095
CMD ["python", "-u", "/app/streamer.py"]
