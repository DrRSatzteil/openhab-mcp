# Use a Python image with Python 3.12
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy requirements and setup files first for better caching
COPY requirements.txt setup.py setup.cfg pyproject.toml ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code (excluding tests)
COPY openhab_mcp/ ./openhab_mcp/

# Install the package in development mode
RUN pip install -e .

# Set environment variables with defaults that can be overridden at runtime
ENV OPENHAB_URL=http://openhab:8080
ENV OPENHAB_API_TOKEN=""
ENV OPENHAB_USERNAME=""
ENV OPENHAB_PASSWORD=""
ENV PYTHONPATH=/app

# Run the MCP server when the container launches
CMD ["python", "-m", "openhab_mcp.openhab_mcp_server"]