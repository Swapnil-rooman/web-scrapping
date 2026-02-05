# Define custom function directory
ARG FUNCTION_DIR="/function"

# Base image with Playwright (this includes Python and browsers)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Install aws-lambda-cpp build dependencies
# (Sometimes required for awslambdaric, though strict binary wheels might exist)
RUN apt-get update && \
    apt-get install -y \
    g++ \
    make \
    cmake \
    unzip \
    libcurl4-openssl-dev

# Create function directory
RUN mkdir -p ${FUNCTION_DIR}

# Copy function code
COPY webscrape.py ${FUNCTION_DIR}
COPY requirements.txt ${FUNCTION_DIR}

# Install Python dependencies
# We install into the function directory or global
RUN pip install --no-cache-dir -r ${FUNCTION_DIR}/requirements.txt --target ${FUNCTION_DIR}

# Install Playwright browsers (critical for Lambda)
RUN playwright install chromium
RUN playwright install-deps chromium

# Install the Lambda Runtime Interface Client (RIC)
# It's already in requirements.txt but just being explicit about its role
# RUN pip install awslambdaric --target ${FUNCTION_DIR}

# Set working directory to function root
WORKDIR ${FUNCTION_DIR}

# Set the entry program to the Lambda RIC
ENTRYPOINT [ "/usr/bin/python3", "-m", "awslambdaric" ]

# Set the CMD to your handler (could also be done as a parameter to ENTRYPOINT)
CMD [ "webscrape.handler" ]
