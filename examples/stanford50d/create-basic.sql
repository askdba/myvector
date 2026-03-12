-- Simplified schema for CI component tests (no query rewrite needed).
-- Works on MySQL 8.0/8.4/9.0. Uses BLOB for wordvec; myvector_construct() produces compatible storage.
create table words50d (
    wordid int auto_increment primary key,
    word varchar(200),
    wordvec blob
);
