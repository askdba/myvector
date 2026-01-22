ARG MYSQL_VERSION=8.0
FROM oraclelinux:8 AS libstdcxx
RUN dnf -y install oraclelinux-developer-release-el8 dnf-plugins-core && \
    dnf config-manager --enable ol8_codeready_builder && \
    dnf -y install gcc-toolset-11 && \
    mkdir -p /opt/libstdcxx && \
    libstdcxx_file="$(find /opt/rh/gcc-toolset-11 -name 'libstdc++.so.*' -type f | sort | tail -n1)" && \
    cp -a "$libstdcxx_file" /opt/libstdcxx/ && \
    ln -s "$(basename "$libstdcxx_file")" /opt/libstdcxx/libstdc++.so.6 && \
    dnf clean all

# Use MySQL as the base image
FROM mysql:${MYSQL_VERSION}

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY myvector.so /usr/lib/mysql/plugin/
RUN if [ -d /usr/lib64/mysql/plugin ]; then \
      cp /usr/lib/mysql/plugin/myvector.so /usr/lib64/mysql/plugin/; \
    fi
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /usr/lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.* /usr/lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /usr/lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.* /usr/lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.* /lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.* /lib/
RUN ldconfig

# The rest will be handled by the default MySQL entrypoint
