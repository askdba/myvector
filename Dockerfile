ARG MYSQL_VERSION=8.0
FROM ubuntu:22.04 AS libstdcxx
RUN apt-get update && apt-get install -y libstdc++6 && rm -rf /var/lib/apt/lists/*

# Use MySQL as the base image
FROM mysql:${MYSQL_VERSION}

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY myvector.so /usr/lib/mysql/plugin/
RUN if [ -d /usr/lib64/mysql/plugin ]; then \
      cp /usr/lib/mysql/plugin/myvector.so /usr/lib64/mysql/plugin/; \
    fi
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/
COPY --from=libstdcxx /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /usr/lib64/
COPY --from=libstdcxx /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /usr/lib/
RUN ldconfig

# The rest will be handled by the default MySQL entrypoint
