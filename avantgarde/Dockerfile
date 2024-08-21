ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY run.sh /

RUN go install howett.net/avantgarde@v0.1.0 && chmod +x /run.sh

CMD [ "/run.sh" ]
