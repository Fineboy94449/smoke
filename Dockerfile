# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ templates/

# Create a directory for the Firebase credentials (will be mounted as secret)
RUN mkdir -p /app/secrets

# Set environment variables
ENV FLASK_APP=app.py
ENV PORT=8080

# Cloud Run requires the app to listen on 0.0.0.0 and the port specified by PORT env var
EXPOSE 8080

# Run gunicorn with appropriate settings for Cloud Run
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 4 --worker-class sync --timeout 60 --access-logfile - --error-logfile - app:app
