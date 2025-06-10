FROM python:3.12.3

# Set work directory
WORKDIR /app

# Copy code
COPY . /app/

# Install Requirements
RUN pip install /app

CMD uvicorn fusionserve.main:app --port 8001 --host 0.0.0.0 --log-config=logging.yaml