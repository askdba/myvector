-- MyVector Plugin Demo.

-- print the word vector for 'harvard'
select myvector_display(wordvec) from  words50d  where word='harvard';

-- select neighbour words to 'harvard'
select word from words50d where MYVECTOR_IS_ANN('test.words50d.wordvec','wordid',myvector_construct('[-0.8597 1.11297 -0.2997 -1.1093 0.15653 -0.13244 -1.05244 -0.92624 -0.52924 -0.24501 -0.22653 0.252993 -0.099125 -0.406425 0.00097853 -0.0358083 -0.1868983 0.7115799 -0.4448983 0.8665198 0.5433998 0.5982698 -0.0315843 -0.4635143 -0.0850383 -1.890238 0.1114238 -0.7560483 -1.696548 -0.3975283 1.297653 -0.3412783 -0.2289783 -1.452478 -0.2985583 -0.2029783 -0.4421183 1.152112 1.505912 -0.4881983 -0.2117683 -0.3618683 -0.0911083 0.9526609 0.2025408 0.1006808 0.6931608 0.2621508 -0.9098683 0.5950769]'));

-- repeat same search with KNN
select word, myvector_distance(wordvec, (select wordvec from words50d where word='harvard')) dist
from words50d
order by dist limit 10;
