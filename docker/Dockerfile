# Use a Python image with Python 3.12
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy requirements, setup files and README for better caching
COPY requirements.txt setup.py setup.cfg pyproject.toml README.md ./

# Install dependencies with --upgrade to ensure latest versions
RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the application code (excluding tests)
COPY openhab_mcp/ ./openhab_mcp/

# Install the package
RUN pip install .

# Set Python path
ENV PYTHONPATH=/app:$PYTHONPATH

# Run the MCP server in the background and keep the container alive
CMD python -m openhab_mcp & \
    tail -f /dev/null