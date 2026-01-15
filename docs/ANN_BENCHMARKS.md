** ANN Benchmark - MyVector vs PGVector vs MariaDB **

_Server_

```
Cloud : OVH
Model Name      : AMD EPYC 9254 24-Core Processor
CPU : 48
RAM : 128GB
```

_gist-960-euclidean_

Index Build (Distance : L2/Euclidean)

| Algorithm |  M  | efconstruction |  Threads  | Build Time |
|-----------|-----|----------------|-----------|------------|
| MyVector  | 24  |   200          | 1         | 24m 37s    |
| MyVector  | 24  |   576          | 1         | 66m 39s    |
| MyVector  | 24  |   800          | 1         | 89m 50s    |
| MyVector  | 16  |   1200         | 24        | 6m 55s     |
| MyVector  | 32  |   800          | 24        | 7m 1s      |
| MyVector  | 32  |   1200         | 48        | 9m 16s     |
| MyVector  | 12  |   2000         | 48        | 8m 8s      |
| PGVector  |     |                |           |            |
| MariaDB   | 24  |     N.A        | 1         | 120m       |


_dbpedia-openai-1000k-angular_

Index Build (Distance : Cosine)

| Algorithm |  M  | efconstruction |  Threads  | Build Time |
|-----------|-----|----------------|-----------|------------|
| MyVector  | 24  |   200          | 1         | 36m 12s    |
| MyVector  | 24  |   400          | 48        | 13m 1s     |
| MariaDB   | 24  |     N.A        | 1         | 98m        |

MariaDB does not have a equivalent efconstruction ->
https://lists.mariadb.org/hyperkitty/list/discuss@lists.mariadb.org/thread/PPRJF4JAFE3RIKMEPAFY2IUJJ4RPHPAW/


ANN Search, k = 10

| Algorithm |  M  | efconstruction | ef search | Recall     |  QPS |
|-----------|-----|----------------|-----------|------------|------|
| MyVector  | 24  |   400          | 10        | 0.851      | 775  |
|           |     |                | 20        | 0.929      | 743  |
|           |     |                | 40        | 0.970      | 800  |
|           |     |                | 80        | 0.988      | 604  |
|           |     |                | 200       | 0.996      | 338  |
|           |     |                | 400       | 0.998      | 205  |
|           |     |                | 800       | 0.999      | 124  |
| Mariadb   | 24  |   N.A          | 10        | 0.992      | 887  |
|           |     |                | 20        | 0.997      | 452  |
|           |     |                | 40        | 0.998      | 260  |
|           |     |                | 80        | 0.999      |  28  |
|           |     |                | 200       | 1.000      |  13  |
|           |     |                | 400       | 1.000      |   3  |
|           |     |                | 800       | 1.000      |   3  |

ANN Search, k = 100

| Algorithm |  M  | efconstruction | ef search | Recall     |  QPS |
|-----------|-----|----------------|-----------|------------|------|
| MyVector  | 24  |   400          | 10        | 0.987      | 419  |
|           |     |                | 20        | 0.987      | 427  |
|           |     |                | 40        | 0.987      | 420  |
|           |     |                | 80        | 0.987      | 421  |
|           |     |                | 200       | 0.999      | 283  |
|           |     |                | 400       | 1.000      | 178  |
| Mariadb   | 24  |   N.A          | 10        | 1.000      |  12  |
