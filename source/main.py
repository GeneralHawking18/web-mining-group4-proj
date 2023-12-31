import torch
from tqdm import tqdm
from model import Transformer
from config import get_config
import torch.nn as nn
from loss_func import CELoss, SupConLoss, DualLoss
from data_utils import load_data, text2dict
from transformers import logging, AutoTokenizer, AutoModel, BertModel, BertConfig, get_linear_schedule_with_warmup
from sklearn.metrics import classification_report, confusion_matrix
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import os

save_path = os.environ['SAVE_MODEL']

class Instructor:

    def __init__(self, args, logger):
        self.args = args
        self.logger = logger
        self.logger.info('> creating model {}'.format(args.model_name))
        #MODEL DEFINE
        if args.model_name == 'bert':
            self.tokenizer = AutoTokenizer.from_pretrained('bert-base-uncased')
            base_model = AutoModel.from_pretrained('bert-base-uncased')
        elif args.model_name == 'phobert':
            # label_dict = text2dict(f"{args.dataset}_label.txt")
            self.tokenizer = AutoTokenizer.from_pretrained('vinai/phobert-base')

            # new_vocab_len = len(self.tokenizer.get_vocab()) + len(list(label_dict.keys()))
            # special_tokens = {"additional_special_tokens": list(label_dict.keys())}
            # self.tokenizer.add_special_tokens(special_tokens)
            base_model = AutoModel.from_pretrained('vinai/phobert-base')
            # base_model.embeddings.word_embeddings = nn.Embedding(new_vocab_len, 768, padding_idx=1)
            
        elif args.model_name == 'roberta':
            self.tokenizer = AutoTokenizer.from_pretrained('roberta-base', add_prefix_space=True)
            base_model = AutoModel.from_pretrained('roberta-base')
        elif args.model_name == 'bert-scratch':
            self.tokenizer = AutoTokenizer.from_pretrained('vinai/phobert-base')
            config = BertConfig.from_pretrained('vinai/phobert-base')
            # add special tokens:
            if args.dataset in ['uit-nlp', 'phoATIS']:
                label_dict = text2dict(f"{args.dataset}_label.txt")
                new_vocab_len = len(self.tokenizer.get_vocab()) + len(list(label_dict.keys()))
                config.vocab_size = new_vocab_len
                special_tokens = {"additional_special_tokens": list(label_dict.keys())}
                self.tokenizer.add_special_tokens(special_tokens)
                base_model = BertModel(config)
                
        else:
            raise ValueError('unknown model')

        self.model = Transformer(base_model, args.num_classes, args.method)
        self.model.to(args.device)
        if args.device.type == 'cuda':
            self.logger.info('> cuda memory allocated: {}'.format(torch.cuda.memory_allocated(args.device.index)))
        self._print_args()

    def _print_args(self):
        self.logger.info('> training arguments:')
        for arg in vars(self.args):
            self.logger.info(f">>> {arg}: {getattr(self.args, arg)}")

    def _train(self, dataloader, criterion, optimizer, scheduler):
        train_loss, n_correct, n_train = 0, 0, 0
        self.model.train()
        for inputs, targets in tqdm(dataloader, disable=self.args.backend, ascii=' >='):
            inputs = {k: v.to(self.args.device) for k, v in inputs.items()}
            targets = targets.to(self.args.device)
            outputs = self.model(inputs)
            loss = criterion(outputs, targets)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            train_loss += loss.item() * targets.size(0)
            n_correct += (torch.argmax(outputs['predicts'], -1) == targets).sum().item()
            n_train += targets.size(0)
        return train_loss / n_train, n_correct / n_train

    def _test(self, dataloader, criterion):
        test_loss, n_correct, n_test = 0, 0, 0
        y_true, y_pred = [], []
        self.model.eval()
        with torch.no_grad():
            for inputs, targets in tqdm(dataloader, disable=self.args.backend, ascii=' >='):
                inputs = {k: v.to(self.args.device) for k, v in inputs.items()}
                targets = targets.to(self.args.device)
                outputs = self.model(inputs)
                loss = criterion(outputs, targets)
                test_loss += loss.item() * targets.size(0)
                # print(targets) #for verify sanity
                n_correct += (torch.argmax(outputs['predicts'], -1) == targets).sum().item()
                y_pred += torch.argmax(outputs['predicts'], -1).cpu().numpy().reshape(-1).tolist()
                y_true += targets.cpu().numpy().reshape(-1).tolist()
                n_test += targets.size(0)
        print(classification_report(y_true, y_pred))
        
    
        #Confusion matrix
        cm = confusion_matrix(y_true, y_pred)
        df_cm = pd.DataFrame(cm, range(5), range(5))
        # plt.figure(figsize=(10,7))
        sns.set(font_scale=1.4) # for label size
        sns.heatmap(df_cm, annot=True, annot_kws={"size": 16}) # font size

        plt.imshow(df_cm)

        return test_loss / n_test, n_correct / n_test

    def run(self):
        train_dataloader, test_dataloader = load_data(dataset=self.args.dataset,
                                                      data_dir=self.args.data_dir,
                                                      tokenizer=self.tokenizer,
                                                      train_batch_size=self.args.train_batch_size,
                                                      test_batch_size=self.args.test_batch_size,
                                                      model_name=self.args.model_name,
                                                      method=self.args.method,
                                                      workers=0)
        _params = filter(lambda p: p.requires_grad, self.model.parameters())
        if self.args.method == 'ce':
            criterion = CELoss()
        elif self.args.method == 'scl':
            criterion = SupConLoss(self.args.alpha, self.args.temp)
        elif self.args.method == 'dualcl':
            criterion = DualLoss(self.args.alpha, self.args.temp)
        else:
            raise ValueError('unknown method')
        optimizer = torch.optim.AdamW(_params, lr=self.args.lr)
        total_steps = len(train_dataloader)*args.num_epoch
        scheduler = get_linear_schedule_with_warmup(optimizer=optimizer, num_training_steps=total_steps, num_warmup_steps=50)
        best_loss, best_acc = 0, 0
        for epoch in range(self.args.num_epoch):
            train_loss, train_acc = self._train(train_dataloader, criterion, optimizer, scheduler)
            test_loss, test_acc = self._test(test_dataloader, criterion)
            if test_acc > best_acc or (test_acc == best_acc and test_loss < best_loss):
                logger.info("BETTER MODEL SAVED")
                torch.save(self.model.state_dict(), f"{save_path}/best_model.mdl")
                best_acc, best_loss = test_acc, test_loss
            self.logger.info('{}/{} - {:.2f}%'.format(epoch+1, self.args.num_epoch, 100*(epoch+1)/self.args.num_epoch))
            self.logger.info('[train] loss: {:.4f}, acc: {:.2f}'.format(train_loss, train_acc*100))
            self.logger.info('[test] loss: {:.4f}, acc: {:.2f}'.format(test_loss, test_acc*100))
        self.logger.info('best loss: {:.4f}, best acc: {:.2f}'.format(best_loss, best_acc*100))
        self.logger.info('log saved: {}'.format(self.args.log_name))


if __name__ == '__main__':
    logging.set_verbosity_error()
    args, logger = get_config()
    ins = Instructor(args, logger)
    ins.run()
