'''
@authors Byron Wallace, Iain Marshall

RoB 2.0: including outcome-type specific decisions.
'''

from __future__ import print_function
import pdb
from collections import defaultdict
import sys 
try:
    reload(sys)
    sys.setdefaultencoding('utf8')
except:
    # almost certainly means Python 3x
    pass 

import random

import numpy as np

from keras import optimizers
from keras.optimizers import SGD, RMSprop
from keras import backend as K 
K.set_image_dim_ordering("th")
K.set_image_data_format("channels_first")

from keras.models import Model, Sequential, model_from_json #load_model
from keras.preprocessing import sequence
from keras.engine.topology import Layer
from keras.preprocessing.sequence import pad_sequences
from keras.layers import Input, Embedding, Dense, merge
from keras.layers.merge import concatenate
from keras.layers.core import Dense, Dropout, Activation, Flatten, Reshape, Permute, Lambda
from keras.layers.wrappers import TimeDistributed
from keras.layers.embeddings import Embedding
from keras.layers.convolutional import Conv1D, Convolution2D, Conv2D, MaxPooling1D, MaxPooling2D
from keras.preprocessing.text import text_to_word_sequence, Tokenizer
from keras.callbacks import ModelCheckpoint, EarlyStopping
from keras.constraints import maxnorm
from keras.regularizers import l2

# OUTCOME_TYPES = ["all", "mortality", "objective", "subjective"]
# 2/26 -- for now, just doing all.
OUTCOME_TYPES = ["all"]

            
DOC_OUTCOMES = ["rsg-doc-judgment"]
SENT_OUTCOMES = ["rsg-rationale"]
'''
DOC_OUTCOMES = ["ac-doc-judgment", "rsg-doc-judgment"] + \
                    ["boa-doc-judgment-{0}".format(outcome_type) for outcome_type in OUTCOME_TYPES] + \
                    ["bpp-doc-judgment-{0}".format(outcome_type) for outcome_type in OUTCOME_TYPES]
            
SENT_OUTCOMES = ["ac-rationale", "rsg-rationale"]  + \
                    ["boa-rationale-{0}".format(outcome_type) for outcome_type in OUTCOME_TYPES] + \
                    ["bpp-rationale-{0}".format(outcome_type) for outcome_type in OUTCOME_TYPES]

'''


class RationaleCNN:

    def __init__(self, preprocessor, filters=None, n_filters=32, 
                        sent_dropout=0.5, doc_dropout=0.5, 
                        end_to_end_train=False, f_beta=2,
                        document_model_architecture_path=None,
                        document_model_weights_path=None):
        '''
        parameters
        ---
        preprocessor: an instance of the Preprocessor class, defined below
        '''
        self.preprocessor = preprocessor

        if filters is None:
            self.ngram_filters = [3, 4, 5]
        else:
            self.ngram_filters = filters 

      
        self.n_filters = n_filters 
        self.sent_dropout = sent_dropout
        self.doc_dropout  = doc_dropout
        self.sentence_model_trained = False 
        self.end_to_end_train = end_to_end_train
        self.sentence_prob_model = None 
        self.f_beta = f_beta

        '''
        if document_model_architecture_path is not None: 
            assert(document_model_weights_path is not None)

            print("loading model architecture from file: %s" % document_model_architecture_path)

            with open(document_model_architecture_path) as doc_arch:
                doc_arch_str = doc_arch.read()
                self.doc_model = model_from_json(doc_arch_str)
            
            self.doc_model.load_weights(document_model_weights_path)

            self.set_final_sentence_model() # setup sentence model, too
            print("ok!")
        '''

    @staticmethod
    def metric_func_maker(metric_name="f", beta=1):
        
        return_recall=False
        return_precision=False
        
        func_name = metric_name
        if metric_name == "recall": 
            return_recall = True 
        elif metric_name == "precision":
            return_precision = True 
        else: 
            func_name = "f_%s" % beta
            
        def f_beta_score(y, y_pred):
            ''' for convienence '''
            y_pred_binary = K.round(y_pred)
            num_true = K.sum(y)
            num_pred = K.sum(y_pred_binary)
            tp = K.sum(y * y_pred_binary)

            recall = K.switch(num_true>0, tp / num_true, 0.0)
            if return_recall:
                return recall

            precision = K.switch(num_pred>0, tp / num_pred, 0.0)
            if return_precision:
                return precision 

            precision_recall_sum = recall + (beta*precision)

            return K.switch(precision_recall_sum>0, 
                             (beta+1)*((precision*recall)/(precision_recall_sum)), 0.0)


        f_beta_score.__name__ = func_name
        return f_beta_score

    @staticmethod
    def get_weighted_sum_func(X, weights):
        # @TODO.. add sentence preds!
        def weighted_sum(X):
            return K.sum(np.multiply(X, weights), axis=-1)
        
        #return K.sum(X, axis=0) 
        return weighted_sum

    @staticmethod
    def weighted_sum_output_shape(input_shape):
        # expects something like (None, max_doc_len, num_features) 
        # returns (1 x num_features)
        shape = list(input_shape)
        return tuple((1, shape[-1]))




    @staticmethod
    def balanced_sample_MT(X, y_sent_lbls_dict, doc_idx, sentences=None, r=1, n_rows=None):
        '''
        This draws a balanced sample of sentences. 

        Note: because of the way the sentence label dictionary is structured, we accept here
        the entire label dict; meaning this ranges over all documents; but this method operates
        over one doc at a time. Its index is doc_idx. 
        '''
        unique_sent_lbls = list(y_sent_lbls_dict)
        all_sent_lbl_vectors = [y_sent_lbls_dict[lbl_type][doc_idx].squeeze() for lbl_type in unique_sent_lbls]
        # sum the binary labels across the sentence label types
        summed_lbl_dict = np.sum(all_sent_lbl_vectors, axis=0)
        _, neg_indices = np.where([summed_lbl_dict <= 0]) 
        _, pos_indices = np.where([summed_lbl_dict > 0])
        sampled_neg_indices = np.random.choice(neg_indices, r*pos_indices.shape[0], replace=False)
        train_indices = np.concatenate([pos_indices, sampled_neg_indices])

        if n_rows is not None:
            # then we will return a matrix comprising n_rows rows, 
            # repeating positive examples but drawing diverse negative
            # instances
            num_rationale_indices = int(n_rows / 2.0)
            rationale_indices = np.random.choice(pos_indices, num_rationale_indices, replace=True)

            # sample the rest as `negative' (neutral) instances
            num_non_rationales = n_rows - num_rationale_indices
            sampled_non_rationale_indices = np.random.choice(neg_indices, num_non_rationales, replace=True)
            train_indices = np.concatenate([rationale_indices, sampled_non_rationale_indices])

        np.random.shuffle(train_indices) # why not

        # now we need to create a label dictionary with these indices
        y_sent_balanced_dict = {}#dict(zip(unique_sent_lbls, [[]]*len(unique_sent_lbls)))
        
        for sent_lbl_type in unique_sent_lbls:
            y_sent_balanced_dict[sent_lbl_type] = y_sent_lbls_dict[sent_lbl_type][doc_idx][train_indices]

        if sentences is not None: 
            return X[train_indices,:], y_sent_balanced_dict, [sentences[idx] for idx in train_indices]

        return X[train_indices,:], y_sent_balanced_dict

    @staticmethod
    def balanced_sample_across_domains(X, y_doc_lbl):
        '''
        Sample and return X', y' such that domains have an equal
        representation of known designations (i.e., the number of unks
        in each domain is roughly balanced)
        '''
        ### 
        # note: y_doc_lbl is a dictionary pointing from domains to 3d 
        # one-hot label vectors
        ###
        domains_to_non_unks = {} # domains -> non-unk indices
        for domain, domain_v in y_doc_lbl.items():
            unk_indicators = domain_v[:, 2]
            # i.e., those that are not unk
            non_missing_for_domain = np.logical_not(unk_indicators).astype(int)
            domains_to_non_unks[domain] = np.where(non_missing_for_domain>0)[0]

        # now sample such that there is equal representation
        # in particular we will sample n non-unk examples
        # per domain (with resplacement)
        m = np.min([v.shape[0] for v in domains_to_non_unks.values()])

        indices = []
        for domain, domain_observed_v in domains_to_non_unks.items():
            indices.extend(np.random.choice(domain_observed_v, m))

        indices = np.array(list(set(indices)))
        y_doc_samples = {}
        for domain in y_doc_lbl: 
            y_doc_samples[domain] = y_doc_lbl[domain][indices]

        #import pdb; pdb.set_trace()
        return X[indices], y_doc_samples



    @staticmethod
    def balanced_sample(X, y, sentences=None, binary=False, k=1, n_rows=None):
        if binary:
            _, neg_indices = np.where([y <= 0]) 
            _, pos_indices = np.where([y > 0])
            sampled_neg_indices = np.random.choice(neg_indices, pos_indices.shape[0], replace=False)
            train_indices = np.concatenate([pos_indices, sampled_neg_indices])
        else:        
            _, pos_rationale_indices = np.where([y[:,0] > 0]) 
            _, neg_rationale_indices = np.where([y[:,1] > 0]) 
            _, non_rationale_indices = np.where([y[:,2] > 0]) 


            if n_rows is not None: 
                # then we will return a matrix comprising n_rows rows, 
                # repeating positive examples but drawing diverse negative
                # instances
                num_rationale_indices = int(n_rows / 2.0)
                if pos_rationale_indices.shape[0] > 0:
                    rationale_indices = np.random.choice(pos_rationale_indices, num_rationale_indices, replace=True)
                else: 
                    rationale_indices = np.random.choice(neg_rationale_indices, num_rationale_indices, replace=True)

                # sample the rest as `negative' (neutral) instances
                num_non_rationales = n_rows - num_rationale_indices
                sampled_non_rationale_indices = np.random.choice(non_rationale_indices, num_non_rationales, replace=True)
                train_indices = np.concatenate([rationale_indices, sampled_non_rationale_indices])
                
            else:

                # sample a number of non-rationales equal to the total
                # number of pos/neg rationales * k
                m = k*(pos_rationale_indices.shape[0] + neg_rationale_indices.shape[0])
                                                # np.array(random.sample(non_rationale_indices, m)) 

                sampled_non_rationale_indices = non_rationale_indices
                if m < non_rationale_indices.shape[0]:
                    sampled_non_rationale_indices = np.random.choice(non_rationale_indices, m, replace=True)

                train_indices = np.concatenate([pos_rationale_indices, neg_rationale_indices, 
                                                    sampled_non_rationale_indices])
            


        np.random.shuffle(train_indices) # why not
        if sentences is not None: 
            return X[train_indices,:], y[train_indices], [sentences[idx] for idx in train_indices]
        return X[train_indices,:], y[train_indices]


    def build_simple_doc_model(self):
        # maintains sentence structure, but does not impose weights.
        tokens_input = Input(name='input', 
                            shape=(self.preprocessor.max_doc_len, self.preprocessor.max_sent_len), 
                            dtype='int32')

        tokens_reshaped = Reshape([self.preprocessor.max_doc_len*self.preprocessor.max_sent_len])(tokens_input)

    
        x = Embedding(self.preprocessor.max_features+1, self.preprocessor.embedding_dims, 
                        weights=self.preprocessor.init_vectors,
                        name="embedding")(tokens_reshaped)

        x = Reshape((1, self.preprocessor.max_doc_len, 
                     self.preprocessor.max_sent_len*self.preprocessor.embedding_dims), 
                     name="reshape")(x)

        convolutions = []

        for n_gram in self.ngram_filters:
            cur_conv = Convolution2D(self.n_filters, 1, 
                                     n_gram*self.preprocessor.embedding_dims, 
                                     subsample=(1, self.preprocessor.embedding_dims),
                                     activation="relu",
                                     name="conv2d_"+str(n_gram))(x)

            # this output (n_filters x max_doc_len x 1)
            one_max = MaxPooling2D(pool_size=(1, self.preprocessor.max_sent_len - n_gram + 1), 
                                   name="pooling_"+str(n_gram))(cur_conv)

            # flip around, to get (1 x max_doc_len x n_filters)
            permuted = Permute((2,1,3), name="permuted_"+str(n_gram)) (one_max)
            
            # drop extra dimension
            r = Reshape((self.preprocessor.max_doc_len, self.n_filters), 
                            name="conv_"+str(n_gram))(permuted)
            
            convolutions.append(r)

        sent_vectors = concatenate(convolutions, name="sentence_vectors")
        sent_vectors = Dropout(self.sent_dropout, name="dropout")(sent_vectors)

        '''
        For this model, we simply take an unweighted sum of the sentence vectors
        to induce a document representation.
        ''' 
        def sum_sentence_vectors(x):
            return K.sum(x, axis=1)

        def sum_sentence_vector_output_shape(input_shape): 
            # should be (batch x max_doc_len x sentence_dim)
            shape = list(input_shape) 
            # something like (None, 96), where 96 is the
            # length of induced sentence vectors
            return (shape[0], shape[-1])
            
        doc_vector = Lambda(sum_sentence_vectors, 
                                output_shape=sum_sentence_vector_output_shape,
                                name="document_vector")(sent_vectors)

        doc_vector = Dropout(self.doc_dropout, name="doc_v_dropout")(doc_vector)
        output = Dense(1, activation="sigmoid", name="doc_prediction")(doc_vector)

        self.doc_model = Model(inputs=tokens_input, outputs=output)

        self.doc_model.compile(metrics=["accuracy",     
                                        RationaleCNN.metric_func_maker(metric_name="f", beta=self.f_beta), 
                                        RationaleCNN.metric_func_maker(metric_name="recall"), 
                                        RationaleCNN.metric_func_maker(metric_name="precision")], 
                                        loss="binary_crossentropy", optimizer="adadelta")
        print("doc-CNN model summary:")
        print(self.doc_model.summary())


    def build_RA_CNN_model(self, domains_to_weights=None):
        # input dim is (max_doc_len x max_sent_len) -- eliding the batch size
        tokens_input = Input(name='input', 
                            shape=(self.preprocessor.max_doc_len, self.preprocessor.max_sent_len), 
                            dtype='int32')
        

        # flatten; create a very wide matrix to hand to embedding layer
        tokens_reshaped = Reshape([self.preprocessor.max_doc_len*self.preprocessor.max_sent_len])(tokens_input)
        # embed the tokens; output will be (p.max_doc_len*p.max_sent_len x embedding_dims)
        # here we should initialize with weights from sentence model embedding layer!
        # also pass weights for initialization
        x = Embedding(self.preprocessor.max_features+1, self.preprocessor.embedding_dims, 
                        name="embedding")(tokens_reshaped)


        # reshape to preserve document structure -> 
        #       (doc_len x (word_in_sent x embedding_dim))

        # the 1 here is a dummy for the `channels' expected
        # by conv2d --> 
        #   (batch, channels, doc_len, (word_in_sent x embedding_dim))
        x = Reshape((1, self.preprocessor.max_doc_len, 
                     self.preprocessor.max_sent_len*self.preprocessor.embedding_dims), 
                     name="reshape")(x)

        total_sentence_dims = len(self.ngram_filters) * self.n_filters 

        convolutions = []
        for n_gram in self.ngram_filters:
            
            cur_conv = Conv2D(self.n_filters, (1, n_gram*self.preprocessor.embedding_dims), 
                                strides=(1, self.preprocessor.embedding_dims),
                                name="conv2d_"+str(n_gram), activation="relu")(x)

            # this output (1 x new_rows x new_cols x n_filters)
            one_max = MaxPooling2D(pool_size=(1, self.preprocessor.max_sent_len-n_gram+1), 
                                   name="pooling_"+str(n_gram))(cur_conv)

            # flip around, to get (1 x max_doc_len x n_filters)
            permuted = Permute((2,1,3), name="permuted_"+str(n_gram)) (one_max)
            
            # drop extra dimension
            r = Reshape((self.preprocessor.max_doc_len, self.n_filters), 
                            name="conv_"+str(n_gram))(permuted)
            
            convolutions.append(r)

        sent_vectors = merge(convolutions, name="sentence_vectors", mode="concat")
 
        ####
        # one (intermediate) output layer per rationale-type
        # @TODO share more within domains? 
        sentence_outputs, sentence_losses = [], []
        self.sentence_layer_names = [] # keep around for later access
        for sent_output_name in SENT_OUTCOMES:
            sent_output_layer = Dense(1, activation="sigmoid", name=sent_output_name)
            # was pre-fixing w/ "sentence_predictions-"
            cur_sent_layer_name = "{0}".format(sent_output_name)
            self.sentence_layer_names.append(cur_sent_layer_name)
            sent_output_preds = TimeDistributed(sent_output_layer, name=cur_sent_layer_name)(sent_vectors)
            sentence_outputs.append(sent_output_preds)
            sentence_losses.append("binary_crossentropy")
        
        outcomes_to_sent_models = dict(zip(SENT_OUTCOMES, sentence_outputs))
        self.sentence_model = Model(inputs=tokens_input, outputs=sentence_outputs)
        self.sentence_model.compile(optimizer="adagrad", loss=sentence_losses, sample_weight_mode="temporal") # "adagrad"
        print (self.sentence_model.summary())


        # these are two helpers for scaling sentence vectors by rationale weights
        def scale_merge(inputs):
            sent_vectors, sent_weights = inputs[0], inputs[1]
            return K.batch_dot(sent_vectors, sent_weights)

        def scale_merge_output_shape(input_shape):
            # this is expected now to be (None x sentence_vec_length x doc_length)
            # or, e.g., (None, 96, 200)
            input_shape_ls = list(input_shape)[0]
            # should be (batch x sentence embedding), e.g., (None, 96)
            return (input_shape_ls[0], input_shape_ls[1])


        # sent vectors will be, e.g., (None, 200, 96)
        # -> reshuffle for dot product below in merge -> (None, 96, 200)
        sent_vectors = Permute((2, 1), name="permuted_sent_vectors")(sent_vectors)

        #####
        # Now, we build a doc-level output for each outcome type
        doc_outputs, doc_losses, doc_loss_weights = [], [], []
        domains_to_weights = {'ac-doc-judgment': 0.063, 
                              'rsg-doc-judgment': 0.332, 
                              'boa-doc-judgment-all': 0.903, 
                              'bpp-doc-judgment-all': 1.0}
        for (sent_output_str, doc_output_str) in zip(SENT_OUTCOMES, DOC_OUTCOMES):
            ## need to access layer named "sentence_predictions-{0}".format(sent_output_name)
            sent_weights_for_outcome = outcomes_to_sent_models[sent_output_str]

            # each outcome type will now get its own doc_vector, as per its weights
            doc_vector_for_outcome = merge([sent_vectors, sent_weights_for_outcome], 
                                        name="doc_vector_{0}".format(doc_output_str),
                                        mode=scale_merge,
                                        output_shape=scale_merge_output_shape)

            # trim extra dim
            doc_vector_for_outcome = Reshape((total_sentence_dims,), name="reshaped_doc_{0}".format(doc_output_str))(doc_vector_for_outcome)
            doc_vector_for_outcome = Dropout(self.doc_dropout, name="doc_v_dropout_{0}".format(doc_output_str))(doc_vector_for_outcome)
            # output space is: [low, high/unclear, missing]
            doc_output_for_outcome = Dense(3, activation="softmax", name="doc_prediction_{0}".format(doc_output_str))(doc_vector_for_outcome)
            doc_outputs.append(doc_output_for_outcome)
            doc_loss_weights.append(domains_to_weights[doc_output_str])
            doc_losses.append("categorical_crossentropy")

        self.doc_model = Model(inputs=tokens_input, outputs=doc_outputs)
        # we use weighted metrics because we mask samples with "unk" for the label; 
        # we therefore incur no penalty for these
        #    metrics=[RationaleCNN.mean_weighted_acc], 

        # 3/19 -- playing with sgd rather than "adam"

        #sgd = optimizers.SGD(lr=0.01, clipvalue=0.5, clipnorm=1.)#, decay=1e-6, momentum=0.9, nesterov=True)

        #self.doc_model.compile(loss=doc_losses, weighted_metrics=['acc'], loss_weights=doc_loss_weights, optimizer=sgd)
        self.doc_model.compile(loss=doc_losses, metrics=['acc'], weighted_metrics=['acc'], loss_weights=doc_loss_weights, optimizer="adam")
        print(self.doc_model.summary())


        self.set_final_sentence_model()

       

        


    def set_final_sentence_model(self):
        '''
        allow convenient access to sentence-level predictions, after training
        '''
        #sent_prob_outputs = self.doc_model.get_layer("sentence_predictions")
        sent_prob_outputs = []

        sent_model = K.function(inputs=self.doc_model.inputs + [K.learning_phase()], 
                        outputs=[self.doc_model.get_layer(sent_layer_name).output for sent_layer_name in self.sentence_layer_names])
        self.sentence_prob_model = sent_model



    # TODO need to force model to not predict unk...
    def calculate_metrics(self, y, y_hat):
        acc_dicts = defaultdict(list)
        for domain in y.keys():

            y_ind = np.argmax(y[domain], axis=1)
            y_hat_ind = np.argmax(y_hat[domain], axis=1)
            not_unk_indices = np.where(y_ind != 2)[0]
            y_ind = y_ind[not_unk_indices]
            y_hat_ind = y_hat_ind[not_unk_indices]

            acc_dicts[domain] = (y_ind == y_hat_ind).sum() / y_ind.shape[0]

            #import pdb; pdb.set_trace()

        #import pdb; pdb.set_trace()
        return acc_dicts

    def predictions_for_docs(self, docs):
        # @TODO this is way slower then it needs to be
        doc_predictions = []
        # this is lame
        output_names = [o.name.split("/")[0] for o in self.doc_model.outputs]
        #for output in self.doc_model.outputs:
        #    predictions_d[output.name] = []

        for doc in docs:
            if doc.sentence_sequences is None:
                # this will be the usual case
                doc.generate_sequences(self.preprocessor)

            X_doc = np.array([doc.get_padded_sequences(self.preprocessor, labels_too=False)])
            
            #doc_pred = self.doc_model.predict(X_doc)[0][0]
            doc_preds = self.doc_model.predict(X_doc)
            doc_preds = dict(zip(output_names, doc_preds))
            doc_predictions.append(doc_preds)
            #for output_layer, pred in zip(self.doc_model.outputs, doc_preds):
            #    predictions_d[output_layer.name].append(pred)

        return doc_predictions

    def predict_and_rank_sentences_for_doc(self, doc, num_rationales=3, threshold=0):
        '''
        Given a Document instance, make doc-level prediction and return
        rationales.
        '''
        # @TODO making two preds seems awkward/inefficient!
        if self.sentence_prob_model is None:
            self.set_final_sentence_model()

        if doc.sentence_sequences is None:
            # this will be the usual case
            doc.generate_sequences(self.preprocessor)

        X_doc = np.array([doc.get_padded_sequences(self.preprocessor, labels_too=False)])
        
        # doc pred
        doc_pred = self.doc_model.predict(X_doc)[0][0]

        # now rank sentences; 0 indicates 'test time'
        sent_preds = self.sentence_prob_model(inputs=[X_doc, 0])[0].squeeze()[:doc.num_sentences]

        # bias_prob = 1 --> low risk 
        # recall: [1, 0, 0] -> positive rationale; [0, 1, 0] -> negative rationale
        idx = 0
        if doc_pred < .5:
            # pick neg rationales
            idx = 1

        rationale_indices = sent_preds[:,idx].argsort()[-num_rationales:]
        rationales = [doc.sentences[r_idx] for r_idx in rationale_indices]

        return (doc_pred, rationales)

    @staticmethod
    def _doc_contains_at_least_one_rationale(sentence_lbl_dicts):
        for y_d in sentence_lbl_dicts:
            if any([y_d_j > 0 for y_d_j in y_d.values()]):
                return True

        return False 




    @staticmethod
    def _combine_dicts(dictionaries, convert_to_np_arrs=False, expand_dims=True, squeeze_dims=False):
        '''
        Merge all dictionaries in list ds into a 
        single dictionary. Assumes these have the same
        set of keys!
        '''
        combined_dict = {}
        keys = dictionaries[0].keys()
        
        for key in keys:
            field_vals = []
            for dict in dictionaries: 
                if key not in dict:
                    print ("Ah! {0} not in {1}".format(key, dict))
                field_vals.append(dict[key])

            if convert_to_np_arrs:
                field_vals = np.array(field_vals)
          
                #if squeeze_dims:
                #    import pdb; pdb.set_trace()
                #    field_vals = field_vals.squeeze()

                if expand_dims:
                    # here we add an extra dim so that the the dimensionality
                    # of this thing will be (num_examples x 1) rather than
                    # just (num_examples,).
                    field_vals = np.expand_dims(field_vals, axis=-1)

            combined_dict[key] = field_vals

        return combined_dict


    @staticmethod
    def mean_weighted_acc(y_true, y_pred):
        import pdb;
        pdb.set_trace()

    @staticmethod
    def _get_val_weights(y_lbl_dict):
        '''
        This assembles a dictionary mapping label types to sample weights, 
        which are set to reflect an equal balance between minority and
        majority instances. In the case where an instances (first dim)
        contains no positive instances, all weights are set to 0, effectively
        ignoring said output for example, at least in the validation set. 
        '''
        y_val_weights = {}
        for lbl_name in y_lbl_dict:
            cur_y_tensor = y_lbl_dict[lbl_name]
            pos_index_tuples = np.where(cur_y_tensor > 0)
            n_pos = pos_index_tuples[0].shape[0]
            if n_pos == 0: 
                # if no positive instances (sentences) exist for this label, 
                # ignore loss here w.r.t. to this output for this instance.
                cur_y_weights = np.zeros(cur_y_tensor.shape[:-1])
            else:
                n_neg = (cur_y_tensor.shape[0] * cur_y_tensor.shape[1]) - n_pos
                pos_weight = n_neg / n_pos 
                cur_y_weights = np.ones(cur_y_tensor.shape[:-1])

                for i,j,k in zip(*pos_index_tuples):
                    # note: k will always be 1; we squeeze it here.
                    cur_y_weights[i,j] = pos_weight
            y_val_weights[lbl_name] = cur_y_weights
        return y_val_weights

    def train_sentence_model(self, train_documents, nb_epoch=5, 
                                downsample=True, 
                                sent_val_split=.2, 
                                sentence_model_weights_path="sentence_model_weights.hdf5"):

        # assumes sentence sequences have been generated!
        assert(train_documents[0].sentence_sequences is not None)

        # for the validation split, we assume this is at the *document*
        # level to be consistent with document-level training. 
        # so if this is .1, for example, the sentences comprising the last 
        # 10% of the documents will be used for validation
        validation_size = int(sent_val_split*len(train_documents))
        print("using sentences from %s docs for sentence prediction validation!" % 
                    validation_size)
    
        # build the train and (nested!) validation sets
        X_doc, y_sent, train_sentences = [], [], []

        y_sent_lbls_dict = {}
        for s_layer_name in self.sentence_layer_names:
            y_sent_lbls_dict[s_layer_name] = []

        for d in train_documents[:-validation_size]:
            cur_X, cur_sent_y_dict = d.get_padded_sequences(self.preprocessor)
            if RationaleCNN._doc_contains_at_least_one_rationale(cur_sent_y_dict):
                X_doc.append(cur_X)
                combined_dict = RationaleCNN._combine_dicts(cur_sent_y_dict)

                for t in combined_dict:
                    y_sent_lbls_dict[t].append(np.array(combined_dict[t]))

                train_sentences.append(d.padded_sentences)
              

        X_doc = np.array(X_doc)
        n_docs, num_sents, sent_len = X_doc.shape
        for outcome_key in y_sent_lbls_dict.keys():
            arr_lbls = np.array(y_sent_lbls_dict[outcome_key])
            y_sent_lbls_dict[outcome_key] = arr_lbls.reshape(n_docs, num_sents, 1)
 
        X_doc_validation, y_sent_dicts_validation, validation_sentences = [], [], []
        for d in train_documents[-validation_size:]:
            cur_X, cur_sent_y_dict = d.get_padded_sequences(self.preprocessor)
            # we only keep validation documents that contain at least one rationale /
            # sentence label 
            # @TODO this results in dropping a *lot* of docs -- needs sanity check
            #       (or perhaps we are doing the matching poorly.)
            if RationaleCNN._doc_contains_at_least_one_rationale(cur_sent_y_dict):
                X_doc_validation.append(cur_X)
                y_sent_dicts_validation.append(RationaleCNN._combine_dicts(cur_sent_y_dict))
                validation_sentences.append(d.padded_sentences)

        print ("using {0} docs for validation that have *any* rationale labels (out of {1} total available validation docs)".format(
                        len(X_doc_validation), validation_size))
        X_doc_validation = np.array(X_doc_validation)
        y_sent_validation = RationaleCNN._combine_dicts(y_sent_dicts_validation, 
                                                            convert_to_np_arrs=True)
        y_sent_validation_weights = RationaleCNN._get_val_weights(y_sent_validation)

        ##############################################################
        # we draw nb_epoch balanced samples; take one pass on each   #
        # here we adopt a 'balanced sampling' approach which entails #
        # including all positive sentence labels (for all domains    #
        # and types), and then r * max(y_k) neg examples, where r is #
        # a hyper-parameter and y_k is a particular label type       #
        ##############################################################

        best_loss = np.inf 
        for iter_ in range(nb_epoch):
            print ("on epoch: %s" % iter_)

            X_temp, sentences_temp = [], []

            # y_sent_temp maps sentence label types to 
            # constructed samples
            y_sent_temp = {}
            for sent_lbl in y_sent_lbls_dict.keys():
                y_sent_temp[sent_lbl] = []

            for i in range(X_doc.shape[0]):
                # i is indexing the document here!
                X_doc_i = X_doc[i]

                '''
                A tricky bit here is that the model expects a given doc length as input,
                so here we take a kind of hacky approach of duplicating the downsampled
                rows per documents. Basically this assembles 'balanced' pseudo documents
                for input to the model.
                '''
                n_target_rows = X_doc_i.shape[0]
                # this will include: all positive sentences (in any domain), and then a matched sample
                # of randomly selected negative ones.
                X_doc_i_temp, y_sent_i_lbl_dict_temp, sampled_sentences = RationaleCNN.balanced_sample_MT(X_doc_i,
                                                                    y_sent_lbls_dict, i, 
                                                                    sentences=train_sentences[i],
                                                                    n_rows=n_target_rows)

                X_temp.append(X_doc_i_temp)
                #import pdb; pdb.set_trace()

                # @TODO note that there will be a pretty big imbalance here
                # w.r.t. sentence label types (domains) that we are not 
                # currently accounting for. may want to do stratified sampling.
                for sent_lbl in y_sent_lbls_dict:
                    y_sent_temp[sent_lbl].append(np.array(y_sent_i_lbl_dict_temp[sent_lbl]))
        
       
                sentences_temp.append(sampled_sentences)

            
            X_temp = np.array(X_temp)
            for sent_lbl in y_sent_lbls_dict:
                y_sent_temp[sent_lbl] = np.array(y_sent_temp[sent_lbl])
             

            self.sentence_model.fit(X_temp, y_sent_temp, epochs=1)

            cur_val_results = self.sentence_model.evaluate(X_doc_validation, y_sent_validation, sample_weight=y_sent_validation_weights)
            if not (type(cur_val_results) == type([])):
              cur_val_results = [None, cur_val_results]
 
            out_str = ["%s: %s" % (metric, val) for metric, val in zip(self.sentence_model.metrics_names, cur_val_results)]
            print ("\n".join(out_str))
            
            # ignore nans; I believe this means we just didn't see any such labels
            # we also skip first entry as this is just a sum of the various losses

            # 3/26 -- previously was skippig first entry!
            # non_nan_val_results = [loss_j for loss_j in cur_val_results[1:] if not np.isnan(loss_j)]
            non_nan_val_results = [loss_j for loss_j in cur_val_results if not np.isnan(loss_j)]
            if len(non_nan_val_results) == 0:
                print("\n\noh dear -- all sentence losses were NaN???!")
            #import pdb; pdb.set_trace()
            total_loss = sum(non_nan_val_results) / len(non_nan_val_results)           
            print ("mean loss: {0}; current best loss: {1}".format(total_loss, best_loss))
            if total_loss < best_loss:
                best_loss = total_loss 
                self.sentence_model.save_weights(sentence_model_weights_path, overwrite=True)
                print("new best sentence loss: %s\n" % best_loss)


        

        # reload best sentence-model weights weights
        self.sentence_model.load_weights(sentence_model_weights_path)
        
        '''
        if not self.end_to_end_train:
            print ("freezing sentence prediction layer weights!")
            sent_softmax_layer = self.doc_model.get_layer("sentence_predictions")
            sent_softmax_layer.trainable = False 

            # after freezing these weights, recompile doc model (as per 
            # https://keras.io/getting-started/faq/#how-can-i-freeze-keras-layers)
            self.doc_model.compile(metrics=["accuracy",     
                                        RationaleCNN.metric_func_maker(metric_name="f", beta=self.f_beta), 
                                        RationaleCNN.metric_func_maker(metric_name="recall"), 
                                        RationaleCNN.metric_func_maker(metric_name="precision")], 
                                        loss="binary_crossentropy", optimizer="adadelta")
        '''

    @staticmethod
    def get_sample_weights_for_docs(lbl_dict, output_weight_dict=None):
        sample_weights_dict = {}
        for lbl in lbl_dict:
            # create a vector such that weights for unk entries are 0.
            cur_y_v = lbl_dict[lbl]
            domain_scalar = 1.0
            if output_weight_dict is not None:
                domain_scalar = output_weight_dict[lbl]

            masked_y = np.ones(cur_y_v.shape[0]) * domain_scalar
            masked_y[np.where(cur_y_v[:,-1] == 1)[0]] = 0 
            sample_weights_dict[lbl] = masked_y
        return sample_weights_dict

    @staticmethod
    def get_per_domain_weights(y_doc_dict):
        '''
        return a dictionary mapping domains to weights,
        which are inversely proportional to the amount of
        data for the respective domain.
        '''
        total_unks = 0
        domains_to_unk_counts = {}
        for domain in y_doc_dict:
            # a vector with [#n_neg, #n_pos, #unk]
            domain_v = np.sum(y_doc_dict[domain], axis=0)
            num_unks_for_domain = domain_v[-1]
            total_unks += num_unks_for_domain
            domains_to_unk_counts[domain] = num_unks_for_domain

        max_unks = np.max(list(domains_to_unk_counts.values()))
        #import pdb; pdb.set_trace()
        # now normalize
        for domain in domains_to_unk_counts:
            domains_to_unk_counts[domain] = domains_to_unk_counts[domain] / max_unks
        return domains_to_unk_counts



    def doc_predict_no_unks(self, X):
        # make predictions; lop off "unk" (last label)
        y_hat = self.doc_model.predict(X) 
        # now remove unk predictions
        y_hat_no_unks = []
        for y_hat_d in y_hat:
            y_hat_no_unks.append(y_hat_d[:,:-1])

        return y_hat_no_unks


    def train_document_model(self, train_documents, nb_epoch=5, downsample=False, 
                                doc_val_split=.2, batch_size=50,
                                document_model_weights_path="document_model_weights.hdf5",
                                pos_class_weight=1):

        validation_size = int(doc_val_split*len(train_documents))
        print("validating using %s out of %s train documents." % (validation_size, len(train_documents)))

        ###
        # build the train set
        ###
        X_doc, y_doc_dicts = [], []
        y_sent = []
        for d in train_documents[:-validation_size]:
            cur_X, cur_sent_y = d.get_padded_sequences(self.preprocessor)
            X_doc.append(cur_X)
            y_doc_dicts.append(d.doc_y_dict)
            y_sent.append(cur_sent_y)
        X_doc = np.array(X_doc)
        y_doc = RationaleCNN._combine_dicts(y_doc_dicts, convert_to_np_arrs=True, 
                                                         expand_dims=False)
        domains_to_weights = RationaleCNN.get_per_domain_weights(y_doc)

        

        ####
        # @TODO refactor (rather redundant with above...)
        # and the validation set. 
        ####
        X_doc_validation, y_doc_validation_dicts = [], []
        y_sent_validation = []
        for d in train_documents[-validation_size:]:
            cur_X, cur_sent_y = d.get_padded_sequences(self.preprocessor)
            X_doc_validation.append(cur_X)
            y_doc_validation_dicts.append(d.doc_y_dict)
            y_sent_validation.append(cur_sent_y)
        X_doc_validation = np.array(X_doc_validation)
        y_doc_validation = RationaleCNN._combine_dicts(y_doc_validation_dicts, 
                                                        convert_to_np_arrs=True, 
                                                        expand_dims=False)


        
        ####
        # 2/22 -- status is that the specialized ('subjective'/'objective') assessments
        # are very sparse. confirming w/iain that this is as expected.
        #
        # one thought: factorize classification in low/high risk and the designation of 
        # objective/v. subjective outcomes
        ####
        

        if downsample:
            print("downsampling!")

            cur_score, best_score = None, -np.inf  

            # then draw nb_epoch balanced samples; take one pass on each
            for iter_ in range(nb_epoch):

                print ("on epoch: %s" % iter_)

                
                X_tmp, y_tmp = RationaleCNN.balanced_sample_across_domains(X_doc, y_doc)
                

                doc_weights_tmp = RationaleCNN.get_sample_weights_for_docs(y_tmp, domains_to_weights)
                self.doc_model.fit(X_tmp, y_tmp, batch_size=batch_size, epochs=1, sample_weight=doc_weights_tmp)
                                         #class_weight={0:1, 1:pos_class_weight})

                '''
                take weighted sum here
                '''
                

                y_hat = self.doc_model.predict(X_doc_validation)

                # drop any unk predictions!
                

                y_hat = self.doc_predict_no_unks(X_doc_validation)

                # need to force pred of either 0/1 -- i.e., no predicting 'unk'!

                o_names = [o.name.split("/Softmax")[0] for o in self.doc_model.outputs]
                y_hat = dict(zip(o_names, y_hat))

                acc_dicts = self.calculate_metrics(y_doc_validation, y_hat)
                cur_score = sum(acc_dicts.values())
                print ("accuracy dicts: {0}".format(acc_dicts))
                print("cur score: {0}".format(cur_score))
                print("best score: {0}".format(best_score))

                if cur_score > best_score:
                    best_score = cur_score
                    self.doc_model.save_weights(document_model_weights_path, overwrite=True)
                    print("new best score!!!: %s\n" % best_score)

                #import pdb; pdb.set_trace()

                '''
                doc_val_weights = RationaleCNN.get_sample_weights_for_docs(y_doc_validation)
                cur_val_results = self.doc_model.evaluate(X_doc_validation, y_doc_validation)
                out_str = ["%s: %s" % (metric, val) for metric, val in zip(self.doc_model.metrics_names, cur_val_results)]
                print ("\n".join(out_str))

                loss, cur_acc, cur_f, cur_recall, cur_precision = cur_val_results                
                if cur_f > best_f:
                    best_f = cur_f
                    self.doc_model.save_weights(document_model_weights_path, overwrite=True)
                    print("new best F: %s\n" % best_f)
                '''

        else:

            import pdb; pdb.set_trace()

            # using accuracy here because balanced(-ish) data is assumed.
            checkpointer = ModelCheckpoint(filepath=document_model_weights_path, 
                                    verbose=1,
                                    monitor="weighted_acc",#monitor="val_doc_prediction_rsg-doc-judgment_acc",
                                    save_best_only=True,
                                    mode="max")#"min")



            doc_val_weights = RationaleCNN.get_sample_weights_for_docs(y_doc_validation)#, domains_to_weights)
            doc_weights = RationaleCNN.get_sample_weights_for_docs(y_doc, domains_to_weights)
            import pdb; pdb.set_trace()
            hist = self.doc_model.fit(X_doc, y_doc, 
                        epochs=nb_epoch, 
                        validation_data=(X_doc_validation, y_doc_validation, doc_val_weights),
                        sample_weight=doc_weights,
                        callbacks=[checkpointer],
                        batch_size=batch_size)


        # reload best weights
        self.doc_model.load_weights(document_model_weights_path)

class Document:
    def __init__(self, doc_id, sentences, doc_lbl_dict=None, 
                    sentence_lbl_dicts=None, min_sent_len=3):
        self.doc_id = doc_id
        self.doc_y_dict = doc_lbl_dict

        ###
        # each sentence is associated with multiple (named) 
        # outputs; these are binary indicators that encode whether
        # said sentence constitutes a rationale for the respective
        # domain RoB judgments.
        ###
        self.sentences, self.sentence_y_dicts = [], []
        for idx, s in enumerate(sentences):
            if len(s.split(" ")) >= min_sent_len:
                self.sentences.append(s)
                if not sentence_lbl_dicts is None:
                    #sent_y_vec = sent_lbl_dict_to_vec(sentences_labels[idx])
                    #self.sentences_y_vects.append(sentences_labels[idx])
                    self.sentence_y_dicts.append(sentence_lbl_dicts[idx])

        self.sentence_sequences = None
        # length, pre-padding!
        self.num_sentences = len(self.sentences)

        self.sentence_weights = None 
        self.sentence_idx = 0
        self.n = len(self.sentences)


    def get_padded_sentences():
        # sometimes useful for indexing purposes
        return self.padded_sentences

    def __len__(self):
        return self.n 

    def generate_sequences(self, p):
        ''' 
        p is a preprocessor that has been instantiated
        elsewhere! this will be used to map sentences to 
        integer sequences here.
        '''
        self.sentence_sequences = p.build_sequences(self.sentences)
        self.padded_sentences = self.sentences + [''] * (p.max_doc_len - self.n)


    def get_padded_sequences_for_X_y(self, p, X, y_dicts):
        n_sentences = X.shape[0]
        y = None
        if n_sentences > p.max_doc_len:
            X = X[:p.max_doc_len]
            y = y_dicts[:p.max_doc_len]
        elif n_sentences <= p.max_doc_len:
            dummy_rows = 0 * np.ones((p.max_doc_len-n_sentences, p.max_sent_len), dtype='int32')
            X = np.vstack((X, dummy_rows))
        
            # for padded rows (which represent sentences), create all-zero label dictionaries
            dummy_sent_lbl_dict = {}
            for sent_output_name in SENT_OUTCOMES:
                cur_sent_layer_name = "{0}".format(sent_output_name)
                dummy_sent_lbl_dict[cur_sent_layer_name] = 0.0 

            dummy_lbls = [dummy_sent_lbl_dict]*(p.max_doc_len-n_sentences)
            y = y_dicts + dummy_lbls
            

        return np.array(X), y

    def get_padded_sequences_for_X(self, p, X):
        n_sentences = X.shape[0]
        if n_sentences > p.max_doc_len:
            X = X[:p.max_doc_len]
        elif n_sentences < p.max_doc_len:
            # pad
            #dummy_rows = p.max_features * np.ones((p.max_doc_len-n_sentences, p.max_sent_len), dtype='int32') 
            dummy_rows = 0 * np.ones((p.max_doc_len-n_sentences, p.max_sent_len), dtype='int32')
            X = np.vstack((X, dummy_rows))
        return np.array(X)


    def get_padded_sequences(self, p, labels_too=True):
        # return p.build_sequences(self.sentences, pad_documents=True)              
        #n_sentences = self.sentence_sequences.shape[0]
        X = self.sentence_sequences

        if labels_too:    
            y_dicts = self.sentence_y_dicts
            return self.get_padded_sequences_for_X_y(p, X, y_dicts)

        # otherwise only return X
        return self.get_padded_sequences_for_X(p, X)

class Preprocessor:
    def __init__(self, max_features, max_sent_len, embedding_dims=200, wvs=None, 
                    max_doc_len=500, stopword=True):
        '''
        max_features: the upper bound to be placed on the vocabulary size.
        max_sent_len: the maximum length (in terms of tokens) of the instances/texts.
        embedding_dims: size of the token embeddings; over-ridden if pre-trained
                          vectors is provided (if wvs is not None).
        '''

        self.max_features = max_features  
        self.tokenizer = Tokenizer(num_words=self.max_features)#num_words=self.max_features)
        self.max_sent_len = max_sent_len  # the max sentence length! 
        self.max_doc_len = max_doc_len # w.r.t. number of sentences!

        self.use_pretrained_embeddings = False 
        self.init_vectors = None 
        if wvs is None:
            self.embedding_dims = embedding_dims
        else:
            # note that these are only for initialization;
            # they will be tuned!
            self.use_pretrained_embeddings = True
            # for new gensim format
            self.embedding_dims = wvs.syn0.shape[1] #wvs.vector_size
            self.word_embeddings = wvs

        
        self.stopword = stopword
        # lifted directly from spacy's EN list
        #self.stopwords = [u'all', u'six', u'just', u'less', u'being', u'indeed', u'over', u'move', u'anyway', u'four', u'not', u'own', u'through', u'using', u'fify', u'where', u'mill', u'only', u'find', u'before', u'one', u'whose', u'system', u'how', u'somewhere', u'much', u'thick', u'show', u'had', u'enough', u'should', u'to', u'must', u'whom', u'seeming', u'yourselves', u'under', u'ours', u'two', u'has', u'might', u'thereafter', u'latterly', u'do', u'them', u'his', u'around', u'than', u'get', u'very', u'de', u'none', u'cannot', u'every', u'un', u'they', u'front', u'during', u'thus', u'now', u'him', u'nor', u'name', u'regarding', u'several', u'hereafter', u'did', u'always', u'who', u'didn', u'whither', u'this', u'someone', u'either', u'each', u'become', u'thereupon', u'sometime', u'side', u'towards', u'therein', u'twelve', u'because', u'often', u'ten', u'our', u'doing', u'km', u'eg', u'some', u'back', u'used', u'up', u'go', u'namely', u'computer', u'are', u'further', u'beyond', u'ourselves', u'yet', u'out', u'even', u'will', u'what', u'still', u'for', u'bottom', u'mine', u'since', u'please', u'forty', u'per', u'its', u'everything', u'behind', u'does', u'various', u'above', u'between', u'it', u'neither', u'seemed', u'ever', u'across', u'she', u'somehow', u'be', u'we', u'full', u'never', u'sixty', u'however', u'here', u'otherwise', u'were', u'whereupon', u'nowhere', u'although', u'found', u'alone', u're', u'along', u'quite', u'fifteen', u'by', u'both', u'about', u'last', u'would', u'anything', u'via', u'many', u'could', u'thence', u'put', u'against', u'keep', u'etc', u'amount', u'became', u'ltd', u'hence', u'onto', u'or', u'con', u'among', u'already', u'co', u'afterwards', u'formerly', u'within', u'seems', u'into', u'others', u'while', u'whatever', u'except', u'down', u'hers', u'everyone', u'done', u'least', u'another', u'whoever', u'moreover', u'couldnt', u'throughout', u'anyhow', u'yourself', u'three', u'from', u'her', u'few', u'together', u'top', u'there', u'due', u'been', u'next', u'anyone', u'eleven', u'cry', u'call', u'therefore', u'interest', u'then', u'thru', u'themselves', u'hundred', u'really', u'sincere', u'empty', u'more', u'himself', u'elsewhere', u'mostly', u'on', u'fire', u'am', u'becoming', u'hereby', u'amongst', u'else', u'part', u'everywhere', u'too', u'kg', u'herself', u'former', u'those', u'he', u'me', u'myself', u'made', u'twenty', u'these', u'was', u'bill', u'cant', u'us', u'until', u'besides', u'nevertheless', u'below', u'anywhere', u'nine', u'can', u'whether', u'of', u'your', u'toward', u'my', u'say', u'something', u'and', u'whereafter', u'whenever', u'give', u'almost', u'wherever', u'is', u'describe', u'beforehand', u'herein', u'doesn', u'an', u'as', u'itself', u'at', u'have', u'in', u'seem', u'whence', u'ie', u'any', u'fill', u'again', u'hasnt', u'inc', u'thereby', u'thin', u'no', u'perhaps', u'latter', u'meanwhile', u'when', u'detail', u'same', u'wherein', u'beside', u'also', u'that', u'other', u'take', u'which', u'becomes', u'you', u'if', u'nobody', u'unless', u'whereas', u'see', u'though', u'may', u'after', u'upon', u'most', u'hereupon', u'eight', u'but', u'serious', u'nothing', u'such', u'why', u'off', u'a', u'don', u'whereby', u'third', u'i', u'whole', u'noone', u'sometimes', u'well', u'amoungst', u'yours', u'their', u'rather', u'without', u'so', u'five', u'the', u'first', u'with', u'make', u'once']
        self.stopwords = ["a", "about", "again", "all", "almost", "also", "although", "always", "among", "an", "and", "another", "any", "are", "as", "at", "b", "be", "because", "been", "before", "being", "between", "both", "but", "by", "c", "can", "could", "did", "do", "d", "does", "each", "either", "enough", "etc", "f", "for", "from", "had", "has", "have", "here", "how", "h", "i", "if", "in", "into", "is", "it", "its", "j", "just", "k", "made", "make", "may", "must", "n", "o", "of", "often", "on", "p", "q", "r", "s", "so", "that", "the", "them", "then", "their", "those", "thus", "to", "t", "u", "use", "used", "v", "w", "x", "y", "z", "we", "was"]


    def remove_stopwords(self, texts):
        stopworded_texts = []
        for text in texts: 
            # note the naive segmentation; although this is same as the 
            # keras module does.
            #stopworded_text = " ".join([t for t in text.split(" ") if not t.lower() in self.stopwords])
            stopworded_text = []
            for t in text.split(" "):
                if not t in self.stopwords:
                    if t.isdigit():
                        t = "numbernumbernumber"
                    stopworded_text.append(t)
            #stopworded_text = " ".join([t for t in text.split(" ") if not t in self.stopwords])
            stopworded_text = " ".join(stopworded_text)
            stopworded_texts.append(stopworded_text)
        return stopworded_texts


    def preprocess(self, all_docs):
        ''' 
        This fits tokenizer and builds up input vectors (X) from the list 
        of texts in all_texts. Needs to be called before train!
        '''
        self.raw_texts = all_docs
        if self.stopword:
            #for text in self.raw_texts: 
            self.processed_texts = self.remove_stopwords(self.raw_texts)
        else:
            self.processed_texts = self.raw_texts

        self.fit_tokenizer()
        if self.use_pretrained_embeddings:
            self.init_word_vectors()


    def fit_tokenizer(self):
        ''' Fits tokenizer to all raw texts; remembers indices->words mappings. '''
        self.tokenizer.fit_on_texts(self.processed_texts)
        self.word_indices_to_words = {}
        for token, idx in self.tokenizer.word_index.items():
            self.word_indices_to_words[idx] = token


    def decode(self, x):
        ''' For convenience; map from word index vector to words'''
        words = []
        for t_idx in x:
            if t_idx == 0:
                words.append("pad")
            else: 
                words.append(self.word_indices_to_words[t_idx])
        return " ".join(words) 

    def build_sequences(self, texts, pad_documents=False):
        processed_texts = texts 
        if self.stopword:
            processed_texts = self.remove_stopwords(texts)

        X = list(self.tokenizer.texts_to_sequences_generator(processed_texts))

        # need to pad the number of sentences, too.
        X = np.array(pad_sequences(X, maxlen=self.max_sent_len))

        return X

    def init_word_vectors(self):
        ''' 
        Initialize word vectors.
        '''
        self.init_vectors = []
        unknown_words_to_vecs = {}
        for t, token_idx in self.tokenizer.word_index.items():
            if token_idx <= self.max_features:
                try:
                    self.init_vectors.append(self.word_embeddings[t])
                except:
                    if t not in unknown_words_to_vecs:
                        # randomly initialize
                        unknown_words_to_vecs[t] = np.random.random(
                                                self.embedding_dims)*-2 + 1

                    self.init_vectors.append(unknown_words_to_vecs[t])

        # init padding token!
        self.init_vectors.append(np.zeros(self.embedding_dims))

        # note that we make this a singleton list because that's
        # what Keras wants. 
        self.init_vectors = [np.vstack(self.init_vectors)]
