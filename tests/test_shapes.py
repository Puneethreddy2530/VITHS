import torch
from transformers import CLIPProcessor, CLIPModel
import warnings
warnings.filterwarnings('ignore')

m=CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
p=CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
t_inp=p(text=['test'], return_tensors='pt')
i_inp=p(images=torch.zeros(3, 224, 224), return_tensors='pt')

t_out=m.get_text_features(**t_inp)
i_out=m.get_image_features(**i_inp)

print("TEXT:")
print("Type:", type(t_out))
if hasattr(t_out, "pooler_output"): print("has pooler output")
if hasattr(t_out, "shape"): print("shape:", t_out.shape)

print("IMAGE:")
print("Type:", type(i_out))
if hasattr(i_out, "pooler_output"): print("has pooler output")
if hasattr(i_out, "shape"): print("shape:", i_out.shape)
