FROM python:3.13-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY tuner.py .
EXPOSE 5004
CMD ["python", "-u", "tuner.py"]
