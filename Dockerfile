FROM python:3.14-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY importarr/ importarr/
RUN pip install --no-cache-dir .

FROM python:3.14-slim
RUN useradd -m importarr
WORKDIR /app
COPY --from=build /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=build /usr/local/bin/importarr /usr/local/bin/importarr
USER importarr
ENV PORT=8323
EXPOSE 8323
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8323/health',timeout=2)"
ENTRYPOINT ["importarr"]
