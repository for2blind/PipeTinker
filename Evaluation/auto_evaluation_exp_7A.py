import os,datetime,time
import pandas as pd
current_path = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_path)
import kubernetes
import uuid
import subprocess, asyncio, requests
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict




kubernetes.config.load_kube_config()
v1 = kubernetes.client.CoreV1Api()
namespace = 'cdgp'
scaling = 'none'
scheduler = 'single'
slo = 10
benchmark_version = '1.0.1.qitian'
benchmark_status_wrk_path="/home/pengshijie/qitian/SiGraphEvaluation/metrics/benchmark_status_wrk_exp_7A.csv"
columns = ['uuid','wrkname','scaling','scheduler','start_time','end_time','slo','collected','csv']
# evaluation_record_path="/home/pengshijie/qitian/SiGraphEvaluation/metrics/evaluation_record_exp_7_300s.csv"
evaluation_record_path="/home/pengshijie/qitian/SiGraphEvaluation/metrics/evaluation_record_exp_7A.csv"

if not os.path.exists(evaluation_record_path):
    evaluation_record = pd.DataFrame(columns=columns)
    evaluation_record.to_csv(evaluation_record_path, index=False)
evaluation_record  = pd.read_csv(evaluation_record_path,names=columns)
function_model_size = pd.read_csv('../benchmark/function_model_size.csv',header=0)

def extract_prefix_and_number(key):
    parts = key.split('-')
    prefix = '-'.join(parts[:-1])
    number = int(parts[-1])
    return prefix, number

def build_url_list(wrkname):
    min_numbers = defaultdict(lambda: float('inf'))
    function_config = function_model_size.copy()
    function_config = function_config.loc[(function_config['benchmark'].str.startswith(wrkname)) | (function_config['benchmark'].str.endswith(wrkname))]
    benchmark_entry = {}
    for index, row in function_config.iterrows():
        if row['entry'] == True:
            if not row["model"] in models:
                continue #only LLAMA
            key = f'{row["model"]}-{row["benchmark"]}'
            benchmark_entry[key] = f'http://172.169.8.253:31112/function/{row["function_name_config"]}.cdgp'
    
    if wrkname == 'whole':
        return benchmark_entry
    
    for key, value in benchmark_entry.items():
        prefix, number = extract_prefix_and_number(key)
        
        # If the current number is smaller than the stored number for this prefix, update it
        if number < min_numbers[prefix]:
            min_numbers[prefix] = number

    new_benchmark_entry = {}
    for key, value in benchmark_entry.items():
        prefix, number = extract_prefix_and_number(key)
        if number == min_numbers[prefix]:
            new_benchmark_entry[key] = value

    return new_benchmark_entry

models = function_model_size['model'].unique().tolist()
# models.remove("LLAMA2KK70") #delete llama 70b
models=["LLAMA"] #only LLAMA
EVALUATE_WORKLOAD = ['ME','LATENCY','PARAM','TIME','whole']
# EVALUATE_WORKLOAD = ['LATENCY','PARAM','TIME']
# EVALUATE_WORKLOAD = ['whole']
benchmarks_path = [os.path.join('/home/pengshijie/qitian/benchmark',model) for model in models]
benchmarks_path = dict(zip(models,benchmarks_path))


wrknames = {}
entry = {}
for wrkname in EVALUATE_WORKLOAD:
    wrknames[wrkname] = list(build_url_list(wrkname).keys())
    entry[wrkname] = list(build_url_list(wrkname).values())

# --------------------------------------------
# 1. Deploy all the functions
# --------------------------------------------
def init_benchmark_status():
    # benchmark_status_wrk_column = [wrkname,model,benchmark,init_time,version,deploy]
    columns = ['wrkname','model','benchmark','init_time','version','deploy']
    benchmark_status = pd.read_csv(benchmark_status_wrk_path,names=columns)
    # if benchmark_version not in benchmark_status['version'].values:
    init_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if len(benchmark_status[benchmark_status['version'] == benchmark_version]) == 0:
        # wrkname,model,benchmark,init_time,version,deploy
        wrk_all = wrknames.copy()
        for wrkname in wrk_all:
            # status = pd.DataFrame(columns=['wrk','model','benchmark','init_time','version','deploy'])
            for model in wrk_all[wrkname]:
                model_name = model.split('-')[0].upper()
                benchmark = '-'.join(model.split('-')[1:])
                status = pd.DataFrame([[wrkname,model_name,benchmark,init_time,benchmark_version,0]],columns= columns)
                benchmark_status = pd.concat([benchmark_status,status])
        benchmark_status.to_csv(benchmark_status_wrk_path,index=False)
init_benchmark_status()

def deploy_model(model,wrkname):
    
    benchmark_status = pd.read_csv(benchmark_status_wrk_path)
    model_name = model.split('-')[0].upper()
    print(model_name)
    benchmark = '-'.join(model.split('-')[1:])
    # if service is already deployed, skip
    if check_if_service_ready(model,wrkname):      
        print(f'{model} is already deployed')
    else:
        model_path = f"{benchmarks_path[model_name]}/{benchmark}"
        if benchmark_status.loc[(benchmark_status['model']==model_name) & (benchmark_status['benchmark']==benchmark) & (benchmark_status['version'] == benchmark_version),'deploy'].values[0] == 1:
            cmd = f'cd {model_path} && bash action_deploy.sh'
            # cmd = f'cd {model_path} && bash action_push.sh'
        else:
            cmd = f'cd {model_path} && bash action_deploy.sh'
            # cmd = f'cd {model_path} && bash action_push.sh && bash action_deploy.sh'
            # cmd = f'cd {model_path} && bash action_push.sh && bash action_delete.sh'
            # cmd = f'cd {model_path} && bash action_push.sh'
            # cmd = f'cd {model_path} && bash action_delete.sh'
        os.system(cmd)
    benchmark_status.loc[(benchmark_status['wrkname'] == wrkname) & (benchmark_status['model']==model_name) & (benchmark_status['benchmark']==benchmark) & (benchmark_status['version'] == benchmark_version),'deploy'] = 1
    benchmark_status.to_csv(benchmark_status_wrk_path,index=False)
    return True

async def release_model(wrkname):
    wrk_all = wrknames.copy()
    for model in wrk_all[wrkname]:
        model_name = model.split('-')[0].upper()
        benchmark = '-'.join(model.split('-')[1:])
        model_path = f"{benchmarks_path[model_name]}/{benchmark}"
        cmd = f'cd {model_path} && faas-cli delete -f config.yml'
        os.system(cmd)



def deploy_wrk(wrkname):
    wrk_all = wrknames.copy()
    models = wrk_all[wrkname]
    with ProcessPoolExecutor() as executor:
        for model in models:
            executor.submit(deploy_model, model, wrkname)

def check_if_service_ready(model, wrkname):
    url_list = build_url_list(wrkname)
    if model == 'GPT':
        model = 'OPT'
    url = url_list[model]
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            print(f'service of {model} is ready')
            return True
        else:
            print(f'service of {model} is not ready, return code: {r.status_code}')
            return False
    except Exception as e:
        print(f'service of {model} is not ready', e)
        return False
    

# using request test if the wrk is ready
async def check_wrk_service_ready(wrkname):
    wrk_all = wrknames.copy()
    models = wrk_all[wrkname].copy()
    while True:
        for model in models:
            status = check_if_service_ready(model, wrkname)
            if status == True:
                models.remove(model)
        if len(models) == 0:
            print(f'All service in wrk {wrkname} is ready')
            break

async def remove_dead_pod(pod_name):
    try:
        pod = v1.read_namespaced_pod(pod_name, namespace)
        if not pod.spec.containers[0].resources.limits.get('nvidia.com/gpu'):
            return
        if not pod.status.container_statuses or not pod.status.container_statuses[0].state.running:
            os.system(f'kubectl delete pod {pod.metadata.name} -n {namespace}')
    except:
        pass

async def check_wrk_pod_ready(wrkname, namespace):
    while True:
        pod_status = {}
        pods = v1.list_namespaced_pod(namespace).items
        for pod in pods:
            if pod.status.container_statuses:
                pod_status[pod.metadata.name] = pod.status.container_statuses[0].ready
            else:
                pod_status[pod.metadata.name] = False

        pod_ready = all(status for status in pod_status.values())
        # delete the pod of status "CrashLoopBackOff"
        if not pod_ready:
            print(f'{datetime.datetime.now()}, pod of {wrkname} is not yet ready')
            for pod in pod_status:
                if not pod_status[pod]:
                    await remove_dead_pod(pod)
        else:
            print(f'{wrkname} is ready')
            return pod_ready
        print(f'wait for 30 seconds to check again')
        await asyncio.sleep(30)


async def start_request_to_wrks(wrkname, scaling, scheduler):
    evaluation_record = pd.read_csv(evaluation_record_path,names=columns)
    if len(evaluation_record[(evaluation_record['wrkname']== wrkname)   & (evaluation_record['scaling'] == scaling) & (evaluation_record['scheduler'] == scheduler)]) > 0:
        print(f'{wrkname} {scaling} {scheduler} has been evaluated')
        return
    start_datetime = datetime.datetime.now()
    estimated_end_time = start_datetime + datetime.timedelta(hours=1)    
    print(f"Workload started at: {start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Estimated end time: {estimated_end_time.strftime('%Y-%m-%d %H:%M:%S')}")

    start_time = time.time().__int__()    
    # print("Usage: python3 round_robbin_wrk.py  <wrkname> <scaling> <scheduler> <slo> <start_time>")
    # cmd = f"python3 workloads/cv_wula_RR.py {wrkname} {scaling} {scheduler} {slo} {start_time}"
    cmd = f"python3 workloads/cv_wula.py {wrkname} {scaling} {scheduler} {slo} {start_time}"
    # cmd = f"python3 workloads/cv_wula.py {wrkname} {scaling} {scheduler} {slo} {start_time}"
    # when workload is finished, record the time
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.wait()
    stdout, stderr = process.communicate()
    print(stdout.decode())
    end_time = time.time().__int__()
    evaluation_uuid = uuid.uuid4()
    resp_path = f'metrics/wula/exp_7A/{wrkname}/{scheduler}/{start_time}/'
    tmp_record = pd.DataFrame([[evaluation_uuid,wrkname,scaling,scheduler,start_time,end_time,slo,0,resp_path]],columns=columns)
    evaluation_record = pd.concat([evaluation_record,tmp_record])
    evaluation_record.to_csv(evaluation_record_path,index=False,header=False)

async def run_wrk_benchmark():
    wrk_all = wrknames.copy()
    for wrkname in wrk_all.keys():
        if len(evaluation_record[(evaluation_record['wrkname']== wrkname) & (evaluation_record['scaling'] == scaling) & (evaluation_record['scheduler'] == scheduler)]) > 0:
            print(f'{wrkname} has been evaluated')
            continue
        deploy_wrk(wrkname)
        print(f'{wrkname} is deployed, sleeping to function ready!')

        await check_wrk_pod_ready(wrkname, namespace)
        await check_wrk_service_ready(wrkname)
        print(f'all service of {wrkname} is ready, start benchmark!')
        await start_request_to_wrks(wrkname, scaling, scheduler)
        # release wrk
        await asyncio.sleep(30)
        await release_model(wrkname)
        await asyncio.sleep(30)




if __name__ == '__main__':
    asyncio.run(run_wrk_benchmark())
    # asyncio.run(release_model('LATENCY'))
    