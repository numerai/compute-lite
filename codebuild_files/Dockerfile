ARG FUNCTION_DIR="/home/app/"
ARG RUNTIME_VERSION="3.9"

# Stage 2 - build function and dependencies
FROM public.ecr.aws/lambda/python:${RUNTIME_VERSION} AS build-image

# Include global args in this stage of the build
ARG FUNCTION_DIR
ARG RUNTIME_VERSION
# Create function directory
RUN mkdir -p ${FUNCTION_DIR}
# Copy handler function and custom_model.py if it exists
COPY lambda_handler.py custom_pipeline.py* ${FUNCTION_DIR}
COPY requirements.txt .
RUN yum update -yqq
RUN yum groupinstall -yqq 'Development Tools'
RUN yum install -yqq \
        git \
        python3-dev \
        awscli \
        libcurl \
        build-base \
        libtool \
        autoconf \
        automake \
        libexecinfo-dev \
        libgomp \
        make \
        cmake \
        build-essential \
        nasm
# Optional – Install the function's dependencies
RUN python${RUNTIME_VERSION} -m pip install -r requirements.txt --target ${FUNCTION_DIR}
# Install Lambda Runtime Interface Client for Python
RUN python${RUNTIME_VERSION} -m pip install awslambdaric --target ${FUNCTION_DIR}



# Stage 3 - final runtime image
# Grab a fresh copy of the Python image
FROM public.ecr.aws/lambda/python:${RUNTIME_VERSION}
# Include global arg in this stage of the build
ARG FUNCTION_DIR
# Set working directory to function root directory
WORKDIR ${FUNCTION_DIR}
RUN yum update -yqq
RUN yum install -yqq \
        git \
        python3-dev \
        awscli \
        libcurl \
        build-base \
        libtool \
        autoconf \
        automake \
        libexecinfo-dev \
        libgomp \
        make \
        cmake \
        build-essential \
        nasm
# Copy in the built dependencies
COPY --from=build-image ${FUNCTION_DIR} ${FUNCTION_DIR}

# (Optional) Add Lambda Runtime Interface Emulator and use a script in the ENTRYPOINT for simpler local runs
ADD https://github.com/aws/aws-lambda-runtime-interface-emulator/releases/latest/download/aws-lambda-rie /usr/bin/aws-lambda-rie
COPY entry.sh /
RUN chmod 755 /usr/bin/aws-lambda-rie /entry.sh
ENTRYPOINT [ "/entry.sh" ]
CMD [ "lambda_handler.run" ]
