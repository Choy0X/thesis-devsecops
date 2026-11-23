"""The model gateway: one fixed local address in front of the language model and the embedding model, both run by llama.cpp, loaded when they are used and unloaded when they are not.

* ``llama``: finding the llama.cpp program, placing a model on the card or the processor, and running one ``llama-server`` child.
* ``scheduler``: which models are loaded, who may use them now, and how the two share one graphics card without aborting anyone.
* ``app``, ``endpoint``, ``client``: the HTTP front (``python -m app.backend.gateway``), the address file the rest of the application reads, and the client it uses.
"""
