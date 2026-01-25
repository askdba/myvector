# MyVector Docker Images

## Location

<https://github.com/askdba/myvector/pkgs/container/myvector>

We have our own dedicated image repository!

The MyVector docker images are built on top of community MySQL docker images
from -

<https://hub.docker.com/r/mysql/mysql-server/>

## What is in the docker image?

Pre-built MyVector plugin (myvector.so) and installation script (myvectorplugin.sql)

```bash
- /usr/lib/mysql/plugin/myvector.so
- /docker-entrypoint-initdb.d/myvectorplugin.sql
```

NOTE: The MySQL image entrypoint will run the SQL script automatically on first
startup. If you need to re-run it manually, use:
`mysql -u root -p < /docker-entrypoint-initdb.d/myvectorplugin.sql`

## Installation

The first step is to run the docker container and get a MySQL instance running.

Please review - <https://dev.mysql.com/doc/refman/8.0/en/linux-installation-docker.html>

TL;DR - If you only want to try out vectors, just run the docker container
and that will start a MySQL instance with a newly initialized data directory.
For advanced options and specifically to run the container against an existing
MySQL database, please thoroughly review the documentation link above.

After the MySQL instance is up, you can run the MyVector installation script
as MySQL root user -

```sql
$ mysql -u root -p
mysql> source /docker-entrypoint-initdb.d/myvectorplugin.sql
```

After the plugin is installed/registered above, please follow the usage
instructions in the main README:

<https://github.com/askdba/myvector#-usage-examples>

## MyVector Demos

<https://github.com/askdba/myvector/tree/main/examples/stanford50d>

## Versions

Docker images for MySQL 8.0.x, 8.4.x, and 9.0.x are available.
