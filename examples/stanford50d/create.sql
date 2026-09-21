-- MyVector Plugin Demo. Create table and load word vectors:
-- https://nlp.stanford.edu/projects/glove/
-- Wikipedia 2014 + Gigaword 5 (6B tokens, 400K vocab, uncased,
-- 50d, 100d, 200d, & 300d vectors, 822 MB download): glove.6B.zip
-- This demo uses the 50 dimension word vectors from above dataset.
--
-- Keep the vector column type exactly as written below: the plugin's DDL rewrite matches
-- the upper-case word followed immediately by an opening parenthesis. A formatter that
-- lower-cases it or adds a space makes the statement a syntax error.

create table words50d (
    wordid int auto_increment primary key,
    word varchar(200),
    wordvec MYVECTOR(type=HNSW,dim=50,size=400000,M=64,ef=100)
);
