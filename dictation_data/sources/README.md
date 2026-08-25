# Local dictionary sources

- `jieba_dict.txt`: Jieba default segmentation dictionary, containing word,
  frequency and part-of-speech fields. Source:
  https://github.com/fxsjy/jieba/blob/master/jieba/dict.txt (MIT project).

The character prompt feature combines this frequency dictionary with the
project's textbook vocabulary. Optional THUOCL and CC-CEDICT downloads are not
required at runtime and were not vendored because the current network download
failed; no partial files are kept.
