# Interface
# bonito view - view a model architecture for a given .toml file and the number of parameters in the network.
# bonito train - train a bonito model.
# bonito evaluate - evaluate a model performance.
# bonito download - download pretrained models and training datasets.
# bonito basecaller - basecaller (.fast5 -> .bam).

# Download data
# bonito download --training
# Save to path /home/xiaopeng/miniconda3/envs/nanokws-env/lib/python3.8/site-packages/bonito/data/ 

# Download models
# bonito download --models --show  # show all available models
# bonito download --models         # download all available models
# Save to path /home/xiaopeng/miniconda3/envs/nanokws-env/lib/python3.8/site-packages/bonito/models/

# Train models from scratch
# bonito train --directory /data/training/ctc-data /data/training/model-dir
bonito train -f --directory data/data/dna_r9.4.1/ --config data/configs/dna_r9.4.1@v3.1.toml results/model-dir
# bonito evaluate --directory data/data/dna_r9.4.1/ results/model-dir

# Fine-tune models
# bonito train --epochs 1 --lr 5e-4 --pretrained dna_r10.4.1_e8.2_400bps_hac@v4.0.0 --directory /data/training/ctc-data /data/training/fine-tuned-model

# DNA basecaller
# bonito basecaller dna_r9.4.1 --save-ctc --reference reference.mmi /data/reads > /data/training/ctc-data/basecalls.sam
# bonito basecaller dna_r10.4.1_e8.2_400bps_hac@v4.0.0 /mnt/data/data_repository/nanopore/RNA/Native_RNA/Fast5/Bham_Run1_20171009_DirectRNA_Multi_fast5 > basecalls.bam

# DNA basecaller with reference
# bonito basecaller dna_r10.4.1_e8.2_400bps_hac@v4.0.0 --reference reference.mmi /data/reads > basecalls.bam

# DNA basecaller with reference and modified base calling
# bonito basecaller dna_r10.4.1_e8.2_400bps_hac@v4.0.0 /data/reads --modified-bases 5mC --reference ref.mmi > basecalls_with_mods.bam