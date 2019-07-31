FROM python:3

WORKDIR /usr/src/app

RUN pip install --no-cache-dir awscli

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ENV PATH="/usr/src/app:${PATH}"
ENV PYTHONPATH="/usr/src/app:${PYTHONPATH}"
ENV GETTY_PIPELINE_INPUT=/data
ENV GETTY_PIPELINE_COMMON_SERVICE_FILES_PATH=/data/common
ENV GETTY_PIPELINE_OUTPUT=/output
ENV GETTY_PIPELINE_TMP_PATH=/tmp
ENV PYTHON=/usr/local/bin/python
ENV LC_ALL="C"
ENV LC_CTYPE="C"

COPY scripts scripts
COPY pipeline pipeline
COPY tests tests
COPY data/common /data/common
COPY data/aata/*.json /data/aata/
COPY Makefile setup.py aata.py pir.py knoedler.py settings.py ./

EXPOSE 8080
VOLUME ["/data"]
VOLUME ["/output"]

CMD [ "make", "pir", "LIMIT=100" ]