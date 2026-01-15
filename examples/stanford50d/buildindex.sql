-- MyVector Plugin Demo. Create table and load words + vector corresponding to :-
-- https://nlp.stanford.edu/projects/glove/
-- Wikipedia 2014 + Gigaword 5 (6B tokens, 400K vocab, uncased, 50d, 100d, 200d, & 300d vectors, 822 MB download): glove.6B.zip
-- This demo uses the 50 dimension word vectors from above dataset.

-- !!!!!!!!Replace database name 'test' if required, uncomment and run
-- call mysql.myvector_index_build('test.words50d.wordvec','wordid');
