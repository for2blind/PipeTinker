from prometheus_api_client import PrometheusConnect
import datetime, time
import pandas as pd
from kubernetes import client, config, watch
# read json config file from 'gss.json'
import json
import os,re
import sys, argparse
config.load_kube_config()
import pytz
import glob

os.chdir(os.path.dirname(os.path.abspath(__file__)))
v1 = client.CoreV1Api()
v1_app = client.AppsV1Api()
api = client.CustomObjectsApi()
# v1beta1 = client.ExtensionsV1beta1Api() 

ret = v1.list_node(watch=False)
node_ip = {}
for i in ret.items:
    node_ip[i.status.addresses[0].address] = i.metadata.name


class MetricCollector:
    def __init__(self):
        self.prom = PrometheusConnect(url="http://172.169.8.253:30500")
        self.columns = ['timestamp','value','metrics', 'instance','ip']
        self.columns_gpu = ['timestamp','value','metrics', 'instance','gpu','modelName']
        self.columns_pod = ['timestamp','value','metrics','function']
        self.mdict_node = {
            'node_gpu_util':'DCGM_FI_DEV_GPU_UTIL',
            'gpu_DRAM_activated':'DCGM_FI_PROF_DRAM_ACTIVE',
            'gpu_tensor_pipe_active':'DCGM_FI_PROF_PIPE_TENSOR_ACTIVE',
            'gpu_men_util':'DCGM_FI_DEV_MEM_COPY_UTIL'
        }

        self.sum_metrics = {
            'energy_consumption':'sum(rate(DCGM_FI_DEV_POWER_USAGE[60s]))',
        }

    def collect_sum_metrics(self, start_time, end_time):
        start_time = datetime.datetime.fromtimestamp(start_time,tz=pytz.UTC)
        end_time = datetime.datetime.fromtimestamp(end_time,tz=pytz.UTC)
        dk_tmp = pd.DataFrame()
        for metric_name in self.sum_metrics.keys():
            query_result = self.prom.custom_query_range(self.sum_metrics[metric_name], end_time=end_time, start_time=start_time, step="5s")
            for m in query_result:
                data_t = pd.json_normalize(m)
                for i,r in data_t.iterrows():
                    df_values = pd.DataFrame(r['values'], columns=['timestamp','value'])
                    dk_tmp = pd.concat([dk_tmp,df_values],axis=0)
                dk_tmp['metrics'] = metric_name
        return dk_tmp
        
    def get_node_metrics(self, start_time, end_time):
        start_time = datetime.datetime.fromtimestamp(start_time,tz=pytz.UTC)
        end_time = datetime.datetime.fromtimestamp(end_time,tz=pytz.UTC)
        Mdata_node = pd.DataFrame(columns=self.columns)
        Mdata_GPU = pd.DataFrame(columns=self.columns)
        for metric_name in self.mdict_node.keys():
            query_result = self.prom.custom_query_range(self.mdict_node[metric_name], end_time=end_time, start_time=start_time, step="5s")
            if 'gpu' in metric_name:
                for m in query_result:
                    data_t = pd.json_normalize(m)
                    dk_tmp = pd.DataFrame(columns=self.columns_gpu)
                    for i,r in data_t.iterrows():
                        df_values = pd.DataFrame(r['values'], columns=['timestamp','value'])
                        df_values['gpu'] = r['metric.gpu']
                        df_values['modelName'] = r['metric.modelName']
                        df_values['instance'] = r['metric.kubernetes_node']
                        dk_tmp = pd.concat([dk_tmp,df_values],axis=0)
                    dk_tmp['metrics'] = metric_name
                    Mdata_GPU = pd.concat([Mdata_GPU,dk_tmp],axis=0)
            else:
                for m in query_result:
                    data_t = pd.json_normalize(m)
                    dk_tmp = pd.DataFrame(columns=self.columns)
                    for i,r in data_t.iterrows():
                        df_values = pd.DataFrame(r['values'], columns=['timestamp','value'])
                        dk_tmp = pd.concat([dk_tmp,df_values],axis=0)
                    dk_tmp['metrics'] = metric_name
                    dk_tmp['ip'] = m['metric']['instance'].split(':')[0]
                    Mdata_node = pd.concat([Mdata_node,dk_tmp],axis=0)
                    Mdata_node['instance'] = Mdata_node['ip'].apply(lambda x: node_ip[x])
        return Mdata_node, Mdata_GPU

    def collect_all_metrics(self, start_time, end_time):
        node_perf, gpu_perf = self.get_node_metrics(start_time, end_time)
        return node_perf, gpu_perf
 


class OpenFaasCollector:
    def __init__(self,fun_name):
        self.prom = PrometheusConnect(url="http://172.169.8.253:31113/", disable_ssl=False) 
        # irate(gateway_functions_seconds_sum{function_name=~'bert-21b-submod-.*-latency-10.cdgp'}[60s])
        self.sum_metrics = {
            'execution_time':f'(irate(gateway_functions_seconds_sum{{function_name=~"{fun_name}"}}[60s]) / irate(gateway_functions_seconds_count{{function_name=~"{fun_name}"}}[60s]))',
            'response':f'irate(gateway_function_invocation_total{{function_name=~"{fun_name}"}}[60s])',
            'scale':f'sum(gateway_service_count{{function_name=~"{fun_name}"}}) by (function_name)/6',
            'rps': f'sum(irate(gateway_function_invocation_total{{function_name=~"{fun_name}"}}[60s])) by (function_name)',
            'queue':f'sum by (function_name) (gateway_function_invocation_started{{function_name=~"{fun_name}"}}) - sum by (function_name) (gateway_function_invocation_total{{function_name=~"{fun_name}"}})',
            'invalid':f'sum by (function_name)(irate(gateway_function_invocation_total{{function_name=~"{fun_name}",code!="200"}}[60s]))',
        }
    def collect_sum_metrics(self, start_time, end_time):
        start_time = datetime.datetime.fromtimestamp(start_time,tz=pytz.UTC)
        end_time = datetime.datetime.fromtimestamp(end_time,tz=pytz.UTC)
        dk = pd.DataFrame()
        for metric_name in self.sum_metrics.keys():
            # print(self.sum_metrics[metric_name])
            query_result = self.prom.custom_query_range(self.sum_metrics[metric_name], end_time=end_time, start_time=start_time, step="5s")
            for m in query_result:
                data_t = pd.json_normalize(m)
                for i,r in data_t.iterrows():
                    df_values = pd.DataFrame(r['values'], columns=['timestamp','value'])
                    df_values['function'] = r['metric.function_name']
                    df_values['metrics'] = metric_name
                    dk = pd.concat([dk,df_values],axis=0)
        return dk


if __name__ == "__main__":
    # uuid,model,benchmark,concurrency,scaling,scheduler,workload,start_time,end_time,slo,collected
    # ee7713db-16bc-4a1a-873d-67d47b780901,LATENCY,baseline,baseline,1682668681,1682669882,10,0
    columns = ['uuid','wrkname','scaling','scheduler','start_time','end_time','slo','collected','csv']
    record_csv='metrics/evaluation_record_exp_4.csv'
    record = pd.read_csv(record_csv, names=columns)    
    mc = MetricCollector()    
    for i,r in record.iterrows():
        if int(r['collected']) == 1:
            continue
        # if r['scaling'] != 'cdgp-x':
        #     continue
        # print(r['start_time'],r['end_time'])
        print(f'collecting {r["wrkname"]}, {r["start_time"]}, {r["end_time"]}, total time: {(r["end_time"]-r["start_time"])/3600}')
        evaluation_id = r['uuid']
        wrkname = r['wrkname']
        scheduler = r['scheduler']

        start_time=r['start_time']-25
        end_time=r['end_time']+25
        prom_data_path = f'metrics/system/exp_4/{wrkname}/{scheduler}/{start_time}'

        if not os.path.exists(prom_data_path):
            os.makedirs(prom_data_path)        

        node_perf_all, gpu_perf_all = mc.collect_all_metrics(start_time=r['start_time']-20,end_time=r['end_time']+20)
        gpu_perf_all['evaluation_id'] = evaluation_id
        gpu_perf_all.to_csv(f'{prom_data_path}/gpu_{evaluation_id}.csv',header=False)

        sum_metrics_all = mc.collect_sum_metrics(start_time=r['start_time']-20,end_time=r['end_time']+20)
        sum_metrics_all['evaluation_id'] = evaluation_id
        sum_metrics_all.to_csv(f'{prom_data_path}/sum_{evaluation_id}.csv',header=False)
        csv_files = glob.glob(os.path.join(r['csv'], '*/*.csv'))
        for csv_file in csv_files:
            file_name = os.path.basename(csv_file)
            df = pd.read_csv(csv_file)
            fun_url=df["URL"].unique().tolist()
            fun_name_list=[re.sub(r'-(\d+)-', r'-.*-', s.split("/")[-1]) for s in fun_url]
            fun_name_list=list(set(fun_name_list))
            for fun_name in fun_name_list: 
                openfaas_prom = OpenFaasCollector(fun_name)
                scale_df = openfaas_prom.collect_sum_metrics(start_time=r['start_time']-20,end_time=r['end_time']+20)
                scale_df['evaluation_id'] = evaluation_id
                scale_df.to_csv(f'{prom_data_path}/openfaas_{evaluation_id}.csv',header=False,mode='a')        
        record.loc[i,'collected'] = 1
        record.to_csv(record_csv,header=False,index=False)
                
        
        