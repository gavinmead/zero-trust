# Unity Catalog OSS 0.7.0 + lab.zt.uc.MinioStsCredentialGenerator (STS credential vending that
# MinIO accepts). Compiled against the server's own classpath and appended to it.
FROM unitycatalog/unitycatalog:v0.7.0 AS build
USER root
WORKDIR /build
COPY src/ src/
RUN javac --release 17 \
      -cp "$(cat /home/unitycatalog/server/target/classpath):/home/unitycatalog/server/target/unitycatalog-server-0.7.0.jar" \
      -d classes $(find src -name '*.java') \
 && jar cf zt-uc-minio.jar -C classes . \
 && cp /home/unitycatalog/server/target/classpath classpath \
 && printf ':%s' /home/unitycatalog/zt/zt-uc-minio.jar >> classpath

FROM unitycatalog/unitycatalog:v0.7.0
COPY --from=build --chown=unitycatalog /build/zt-uc-minio.jar /home/unitycatalog/zt/zt-uc-minio.jar
COPY --from=build --chown=unitycatalog /build/classpath /home/unitycatalog/server/target/classpath
