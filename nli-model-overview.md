# NLI models
## Comparison

| Model | Base architecture | Training or fine-tuning data described by the model card | Approximate size |
| --- | --- | --- | --- |
| `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` | mDeBERTa-v3 base | Multilingual NLI data, including a large 2.7 million pair training setup | Base-sized, about 0.3B |
| `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | mDeBERTa-v3 base | MNLI training data plus XNLI development data | Base-sized, about 0.3B |
| `joeddav/xlm-roberta-large-xnli` | XLM-RoBERTa large | Fine-tuned on NLI data in 15 languages, based on multilingual XLM-R pretraining | Large, about 0.6B parameters |
| `vicgalle/xlm-roberta-large-xnli-anli` | XLM-RoBERTa large | MNLI, XNLI, and ANLI, with an emphasis on harder adversarial examples | Large, about 0.6B parameters |
| `MoritzLaurer/multilingual-MiniLMv2-L12-mnli-xnli` | Multilingual MiniLM | MNLI training data plus XNLI development data | Smaller than the base, about 0.1B |
| `symanto/sn-xlm-roberta-base-snli-mnli-anli-xnli` | XLM-RoBERTa base | SNLI, MNLI, ANLI, and XNLI | Base, about 0.3B parameters |
## Links

### `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`

**Hugging Face:** [MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7)


### `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`

**Hugging Face:** [MoritzLaurer/mDeBERTa-v3-base-mnli-xnli](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli)

### `joeddav/xlm-roberta-large-xnli`

**Hugging Face:** [joeddav/xlm-roberta-large-xnli](https://huggingface.co/joeddav/xlm-roberta-large-xnli)

### `vicgalle/xlm-roberta-large-xnli-anli`

**Hugging Face:** [vicgalle/xlm-roberta-large-xnli-anli](https://huggingface.co/vicgalle/xlm-roberta-large-xnli-anli)

### `MoritzLaurer/multilingual-MiniLMv2-L12-mnli-xnli`



**Hugging Face:** [MoritzLaurer/multilingual-MiniLMv2-L12-mnli-xnli](https://huggingface.co/MoritzLaurer/multilingual-MiniLMv2-L12-mnli-xnli)

### `symanto/sn-xlm-roberta-base-snli-mnli-anli-xnli`

**Hugging Face:** [symanto/sn-xlm-roberta-base-snli-mnli-anli-xnli](https://huggingface.co/symanto/sn-xlm-roberta-base-snli-mnli-anli-xnli)

## Keywords

- **SNLI:** The Stanford Natural Language Inference corpus. It contains sentence pairs labeled as entailment, contradiction, or neutral.
- **MNLI:** Multi-Genre Natural Language Inference. It broadens NLI beyond a single style of English text by using multiple genres.
- **XNLI:** Cross-lingual NLI. It extends the NLI task across languages, including professionally translated evaluation examples.
- **ANLI:** Adversarial NLI. It was designed through an adversarial process to create harder examples that expose weaknesses in ordinary NLI systems.

The same dataset name does not mean every model used it in the same way. Training split, evaluation split, language coverage, fine-tuning order, and hyperparameters can all affect the final behavior.

