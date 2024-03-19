import torch
import os
current_directory = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_directory)

with open('../captions.txt', 'r', encoding='utf-8') as file:
  captions = file.read()

print(len(captions)/1e6, 'million letters')

import tiktoken

tokenizer = tiktoken.get_encoding("p50k_base")
tokenizer = tiktoken.encoding_for_model("text-davinci-003")

vocab_size = tokenizer.n_vocab

# Train and test splits
data = torch.tensor(tokenizer.encode(captions), dtype=torch.long)
n = int(0.9*len(data)) # first 90% will be train, rest val
train_data = data[:n]
val_data = data[n:]

import math
import torch.nn as nn
from torch.nn import functional as F

device = 'cuda' if torch.cuda.is_available() else 'cpu'

# hyperparams
batch_size = 8
block_size = 16
max_iters = 100
eval_interval = 10
learning_rate = 1e-6
eval_iters = 5
d_model = 64
n_layers = 8
n_head = 8
dropout = 0.2
norm_eps = 1e-5

torch.manual_seed(1400)
# data loading
def get_batch(split):
    # generate a small batch of data of inputs x and targets y
    data = train_data if split == 'train' else val_data
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([data[i:i+block_size] for i in ix])
    y = torch.stack([data[i+1:i+block_size+1] for i in ix])
    x, y = x.to(device), y.to(device)
    return x, y

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

class AttentionHead(nn.Module):
  """ single head of self attention """

  def __init__(self, d_model, head_size, dropout, block_size):
    super().__init__()
    self.key = nn.Linear(d_model, head_size, bias=True)
    self.query = nn.Linear(d_model, head_size, bias=True) 
    self.value = nn.Linear(d_model, head_size, bias=False)
    self.dropout = nn.Dropout(dropout)
    self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))
    
    # relative positional encoding parameters
    self.max_relative_pos = block_size
    self.relative_embeddings = nn.Embedding(2 * self.max_relative_pos + 1, head_size)
  
  def forward(self, x, mask=False):
    B, T, C = x.shape
    key = self.key(x)
    query = self.query(x)

    # compute relative positions
    positions = torch.arange(T, device=x.device).unsqueeze(0).expand(B, -1)
    relative_positions = positions[:, :, None] - positions[:, None, :]
    clipped_positions = torch.clamp(relative_positions, -self.max_relative_pos, self.max_relative_pos)

    relative_embeddings = self.relative_embeddings(clipped_positions + self.max_relative_pos)
    query_with_pos = query + relative_embeddings
    key_with_pos = key + relative_embeddings.transpose(1, 2)

    # weights = query @ key.transpose(-2, -1) / (key.shape[-1]**-0.5)
    weights = query_with_pos @ key_with_pos.transpose(-2, -1) / math.sqrt(query.shape[-1])

    if mask is True:
      weights = weights.masked_fill(self.tril[:T, :T] == 0, float('-inf')) # (B, T, T)
    
    weights = F.softmax(weights, dim=-1)
    weights = self.dropout(weights)

    value = self.value(x)
    out = weights @ value
    return out

class MultiHeadAttention(nn.Module):
  def __init__(self, d_model, n_head, dropout, block_size):
    head_size = d_model // n_head
    super().__init__()
    self.heads = nn.ModuleList([AttentionHead(d_model=d_model, dropout=dropout, head_size=head_size, block_size=block_size) for _ in range(n_head)])
    self.proj = nn.Linear(n_head * head_size, d_model)
    self.dropout = nn.Dropout(dropout)
  
  def forward(self, x, mask):
    out = torch.cat([h(x, mask=mask) for h in self.heads], dim=-1)
    out = self.dropout(self.proj(out))
    return out

class FeedForward(nn.Module):
  def __init__(self, d_model, dropout):
    super().__init__()
    self.net = nn.Sequential(
      nn.Linear(d_model, 4*d_model),
      nn.GELU(),
      nn.Linear(4*d_model, d_model),
      nn.Dropout(dropout)
    )
   
  def forward(self, x):
    return self.net(x)

class Block(nn.Module):
  def __init__(self, d_model, n_head, norm_eps, dropout):
    super().__init__()
    self.sa_masked = MultiHeadAttention(n_head=n_head, d_model=d_model, dropout=dropout)
    self.ffwd = FeedForward(d_model, dropout=dropout)
    self.norm1 = nn.LayerNorm(d_model, eps=norm_eps)
    self.norm2 = nn.LayerNorm(d_model, eps=norm_eps)
  
  def forward(self, x):
    x2 = x + self.sa_unmasked(self.norm1(x))
    x = x2 + self.ffwd(self.norm2(x2))

    x2 = x + self.sa_masked(self.norm1(x))
    x = x2 + self.ffwd(self.norm2(x2))
    return x

class EncoderNetwork(nn.Module):
  def __init__(self, d_model, n_head, norm_eps, dropout, block_size):
    super().__init__()
    self.s_att = MultiHeadAttention(n_head=n_head, d_model=d_model, dropout=dropout, block_size=block_size)
    self.ffwd = FeedForward(d_model, dropout)
    self.dropout = nn.Dropout(dropout)
    self.norm1 = nn.LayerNorm(d_model, eps=norm_eps)
    self.norm2 = nn.LayerNorm(d_model, eps=norm_eps)
  
  def forward(self, src):
    src2 = self.s_att(src, mask=False)
    src = src + self.dropout(src2)
    src = self.norm1(src)

    src2 = self.ffwd(src)
    src = src + self.dropout(src2)
    src = self.norm2(src)

    return src

class DecoderNetwork(nn.Module):
  def __init__(self, d_model, n_head, norm_eps, dropout, block_size):
    super().__init__()
    self.s_att = MultiHeadAttention(n_head=n_head, d_model=d_model, dropout=dropout, block_size=block_size)
    self.ffwd = FeedForward(d_model, dropout)
    self.dropout = nn.Dropout(dropout)
    self.norm1 = nn.LayerNorm(d_model, eps=norm_eps)
    self.norm2 = nn.LayerNorm(d_model, eps=norm_eps)
  
  def forward(self, src, trg):
    src2 = self.s_att(src, mask=True)
    src = src + self.dropout(src2)
    src = src + self.norm1(src)

    trg2 = self.s_att(trg, mask=False)
    trg = trg + self.dropout(trg2)
    trg = trg + self.norm1(trg)
    
    src_f = src + trg
    src_f2 = self.ffwd(self.norm2(src_f))
    src_f = src_f + self.dropout(src_f2)
    src_f = self.norm2(src_f)

    return src_f

class Transformer(nn.Module):
  def __init__(self):
    super().__init__()
    self.toked_model = nn.Embedding(vocab_size, d_model)
    self.pos_encod = nn.Embedding(block_size, d_model)
    self.block = nn.Sequential(*[Block(d_model=d_model, dropout=dropout, norm_eps=norm_eps, n_head=n_head) for _ in range(n_layers)])
    self.enc_layer = nn.ModuleList([EncoderNetwork(n_head=n_head, norm_eps=norm_eps, block_size=block_size, dropout=dropout, d_model=d_model) for _ in range(n_layers)])
    self.dec_layer = nn.ModuleList([DecoderNetwork(n_head=n_head, norm_eps=norm_eps, block_size=block_size, dropout=dropout, d_model=d_model) for _ in range(n_layers)])

    self.norm_final = nn.LayerNorm(d_model)
    self.linear_final = nn.Linear(d_model, vocab_size)
    self.dropout = nn.Dropout(dropout)
    self.apply(self._init_weights)

  def _init_weights(self, module):
    if isinstance(module, nn.Linear):
      torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
      if module.bias is not None:
        torch.nn.init.zeros_(module.bias.data)
    elif isinstance(module, nn.Embedding):
      torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
    
  def forward(self, idx, targets=None):
    B, T = idx.shape

    toked_model = self.toked_model(idx)
    pos_encod = self.pos_encod(torch.arange(T, device=device))
    x = toked_model + pos_encod

    for layer in self.enc_layer:
      x = layer(x, None)
        
    for layer in self.dec_layer:
      x = layer(x, x)
    
    x = self.norm_final(x)
    logits = self.linear_final(x)

    if targets is None:
      loss = None
    
    else:
      B, T, C = logits.shape
      logits = logits.view(B*T, C)
      targets = targets.view(B*T)
      loss = F.cross_entropy(logits, targets)
    
    return logits, loss
  
  def generate(self, idx, max_new_tokens):
    for _ in range(max_new_tokens):
      idx_cond = idx[:, -block_size:]
      logits, loss = self(idx_cond)
      logits = logits[:, -1, :] # becomes (B, C)
      probs = F.softmax(logits, dim=-1) # (B, C)
      idx_next = torch.multinomial(probs, num_samples=1) # (B, 1)
      idx = torch.cat((idx, idx_next), dim=1) # (B, T+1)

    return idx

model = Transformer()
m = model.to(device)

# no of parameters
n_param = sum(p.numel() for p in m.parameters())/1e6
print(f"{n_param:.2f} million")

# optimizer
optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
steps = []
train_losses = []
val_losses = []

for iter in range(max_iters):

  if iter % eval_interval == 0 or iter == max_iters - 1:
    losses = estimate_loss()
    print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    steps.append(iter)
    train_losses.append(losses['train'])
    val_losses.append(losses['val'])

  xb, yb = get_batch('train')
  logits, loss = model(xb, yb)
  optimizer.zero_grad(set_to_none=True)
  loss.backward()
  optimizer.step()