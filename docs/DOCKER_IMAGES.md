
**MyVector Docker Images**

_Location_

https://hub.docker.com/repository/docker/pulbdb/myvector/

We have our own dedicated image repository! 

The MyVector docker images are built on top of community MySQL docker images from -

https://hub.docker.com/r/mysql/mysql-server/

_What is in the docker image?_

Pre-built MyVector plugin (myvector.so) and installation script (myvectorplugin.sql)

```
-  /usr/lib64/mysql/plugin/myvector.so
-  /usr/share/mysql-<version>/myvectorplugin.sql
-  /usr/share/mysql-<version>/myvector.README
```

NOTE NOTE : Detailed instructions are present inside the Docker image in :
/usr/share/mysql-<version>/myvector.README

_Installation_

The first step is to run the docker container and get MySQL instance running.

Please review - https://dev.mysql.com/doc/refman/8.0/en/linux-installation-docker.html

TL;DR - If you only want to try out vectors, just run the docker container
and that will start a MySQL instance with newly initialized data directory and empty root
password. For advanced options and specifically to run the container against an existing
MySQL database, please thoroughly review the documentation link above.

After the MySQL instance is up, just run the MyVector installation script as MySQL root user -

```
$ mysql <root user>

mysql> source /usr/share/mysql-<version>/myvectorplugin.sql
```

After the plugin is installed/registered above, please follow the CONFIGURE instructions from -

https://github.com/p3io/myvector-dev/blob/main/README.md

_MyVector Demos_

https://github.com/p3io/myvector-dev/tree/main/demo/stanford50d

_Versions_

Docker images for MySQL 8.0.41, 8.4.4, 9.2 are available.


