"""Fine-tuning library shared by the local runner, the Colab/Kaggle notebooks and
the backend. Importing this package must stay cheap: it never imports torch.
Heavy modules (model loading, trainer) import torch themselves when used."""
