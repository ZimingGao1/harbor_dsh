FROM python:3.12.10-slim
COPY src/dsh_tb_agent/model_proxy.py /app/model_proxy.py
USER 10001:10001
ENTRYPOINT ["python", "-B", "/app/model_proxy.py"]
