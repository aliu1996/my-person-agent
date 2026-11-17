FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir matplotlib==3.10.8 openpyxl==3.1.5 python-pptx==1.0.2

COPY deploy/sandbox/run.py /opt/pa/run.py
ENV MPLBACKEND=Agg PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/pa/run.py"]
