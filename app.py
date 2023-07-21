import openai
import json
import ast
import os
import chainlit as cl
from functions.FunctionManager import FunctionManager
import inspect
import tiktoken
import importlib
import asyncio
from functions.MakeRequest import make_request, make_request_chatgpt_plugin
import globale_values as gv

openai.api_key = os.environ.get("OPENAI_API_KEY")
openai.api_base = os.environ.get("OPENAI_API_BASE")

plugin_dirs = [
  d for d in os.listdir('plugins')
  if os.path.isdir(os.path.join('plugins', d)) and d != '__pycache__'
]

functions = []
for dir in plugin_dirs:
  try:
    with open(f'plugins/{dir}/config.json', 'r') as f:
      config = json.load(f)
    enabled = config.get('enabled', True)
  except FileNotFoundError:
    enabled = True

  if not enabled:
    continue

  module = importlib.import_module(f'plugins.{dir}.functions')
  functions.extend([
    obj for name, obj in inspect.getmembers(module) if inspect.isfunction(obj)
  ])

function_manager = FunctionManager(functions=functions)
print("functions:", function_manager.generate_functions_array())

env_max_tokens = os.environ.get("MAX_TOKENS", None)
if env_max_tokens is not None:
  max_tokens = int(env_max_tokens)
else:
    max_tokens = 5000
is_stop = False


def __truncate_conversation(conversation):
  system_con = conversation[0]
  conversation = conversation[1:]
  while True:
    if (get_token_count(conversation) > max_tokens and len(conversation) > 1):
      conversation.pop(1)
    else:
      break
  conversation.insert(0, system_con)
  return conversation


def get_token_count(conversation):
  encoding = tiktoken.encoding_for_model(os.environ.get("OPENAI_MODEL") or "gpt-4")

  num_tokens = 0
  for message in conversation:
    num_tokens += 4
    for key, value in message.items():
      num_tokens += len(encoding.encode(str(value)))
      if key == "name":
        num_tokens += -1
  num_tokens += 2
  return num_tokens


MAX_ITER = 100


async def on_message(user_message: object):
  global is_stop
  print("==================================")
  print(user_message)
  print("==================================")
  user_message = str(user_message)
  message_history = cl.user_session.get("message_history")
  message_history.append({"role": "user", "content": user_message})

  cur_iter = 0
  
  err_count = 0

  while cur_iter < MAX_ITER:

    openai_message = {"role": "", "content": ""}
    function_ui_message = None
    content_ui_message = cl.Message(content="")
    stream_resp = None
    send_message = __truncate_conversation(message_history)
    try:
      functions = function_manager.generate_functions_array()
      user_plugin_api_info = cl.user_session.get('user_plugin_api_info')
      if user_plugin_api_info is not None:
        for item in user_plugin_api_info:
          for i in item['api_info']:
            functions.append(i)
      print("functions:", functions)
      async for stream_resp in await openai.ChatCompletion.acreate(
          model=os.environ.get("OPENAI_MODEL") or "gpt-4",
          messages=send_message,
          stream=True,
          function_call="auto",
          functions=functions,
          temperature=0):  # type: ignore
        new_delta = stream_resp.choices[0]["delta"]
        if is_stop:
          is_stop = False
          cur_iter = MAX_ITER
          break
        openai_message, content_ui_message, function_ui_message = await process_new_delta(
          new_delta, openai_message, content_ui_message, function_ui_message)
    except Exception as e:
      print(e)
      cur_iter += 1
      err_count += 1
      if err_count > 4:
        cl.user_session.set("user_plugin_api_info", None)
        # await cl.Message(
        #     author="system",
        #     content="您的插件已经失效，已经为您清除,请重新绑定其他插件",
        #     indent=1,
        #     language="json",
        #     ).send()
        break
      await asyncio.sleep(1)
      continue

    if stream_resp is None:
      await asyncio.sleep(2)
      continue

    message_history.append(openai_message)
    if function_ui_message is not None:
      await function_ui_message.send()

    if stream_resp.choices[0]["finish_reason"] == "stop":
      break
    elif stream_resp.choices[0]["finish_reason"] != "function_call":
      raise ValueError(stream_resp.choices[0]["finish_reason"])

    function_name = openai_message.get("function_call").get("name")
    print(openai_message.get("function_call"))
    function_response = ""
    try:
      arguments = json.loads(
        openai_message.get("function_call").get("arguments"))
    except:
      arguments = ast.literal_eval(
        openai_message.get("function_call").get("arguments"))
    try:
      function_response = await function_manager.call_function(
        function_name, arguments)
    except Exception as e:
      print(e)
      try:
        # 分割function_name,_分割,如果个数不是3个，就报错
        function_name_split = function_name.split('_')
        if len(function_name_split) < 3:
          print('function_name_split is not 3')
          is_gpt_plugin = False
          if gv.chatgpt_plugin_info is None:
            with open('plugins/serverplugin/my_apis.json', 'r') as f:
              gv.chatgpt_plugin_info = json.load(f)
          plugin_info = gv.chatgpt_plugin_info
          print('共有{}个插件'.format(len(plugin_info)))
          for item in plugin_info:
            for api in item['apis']:
              if api['name'] == function_name:
                id = item['id']
                name = api['name']
                arguments = json.dumps(arguments)
                function_response = make_request_chatgpt_plugin(
                  id, name, arguments)
                is_gpt_plugin = True
                break
            if is_gpt_plugin:
              break
        else:
            method = function_name_split[-2]
            url_md5 = function_name_split[-1]
            request_function_name = '_'.join(function_name_split[:-2])
            print(method, url_md5, request_function_name)
            # 通过url_md5去获取url
            if user_plugin_api_info is None:
                raise Exception('user_plugin_api_info is None')
            for item in user_plugin_api_info:
                print(item)
                if item['url_md5'] == url_md5:
                    url = item['url']
                    function_response = make_request(url, method,
                                                    request_function_name,
                                                    arguments)
                    print(function_response)
                    # 如果是
                    if isinstance(function_response, (tuple, list, dict)):
                        function_response = json.dumps(function_response)
                    break

      except Exception as e:
        print(e)
        break
    print("==================================")
    print(function_response)
    if type(function_response) != str:
      function_response = str(function_response)
    message_history.append({
      "role": "function",
      "name": function_name,
      "content": function_response,
    })
    print("==================================")
    print(message_history)
    print("==================================")

    await cl.Message(
      author=function_name,
      content=str(function_response),
      language="json",
      indent=1,
    ).send()
    cur_iter += 1


async def process_new_delta(new_delta, openai_message, content_ui_message,
                            function_ui_message):
  if "role" in new_delta:
    openai_message["role"] = new_delta["role"]
  if "content" in new_delta:
    new_content = new_delta.get("content") or ""
    openai_message["content"] += new_content
    await content_ui_message.stream_token(new_content)
  if "function_call" in new_delta:
    if "name" in new_delta["function_call"]:
      openai_message["function_call"] = {
        "name": new_delta["function_call"]["name"]
      }
      await content_ui_message.send()
      function_ui_message = cl.Message(
        author=new_delta["function_call"]["name"],
        content="",
        indent=1,
        language="json")
      await function_ui_message.stream_token(new_delta["function_call"]["name"]
                                             )

    if "arguments" in new_delta["function_call"]:
      if "arguments" not in openai_message["function_call"]:
        openai_message["function_call"]["arguments"] = ""
      openai_message["function_call"]["arguments"] += new_delta[
        "function_call"]["arguments"]
      await function_ui_message.stream_token(
        new_delta["function_call"]["arguments"])
  return openai_message, content_ui_message, function_ui_message


@cl.on_chat_start
async def start_chat():
    content = """
       Assistant is designed to be able to assist with a wide range of tasks, from answering simple questions to providing in-depth explanations and discussions on a wide range of topics. 
        As a language model, Assistant is able to generate human-like text based on the input it receives, allowing it to engage in natural-sounding conversations and provide responses that are coherent and relevant to the topic at hand.
        Assistant is constantly learning and improving, and its capabilities are constantly evolving. 
        It is able to process and understand large amounts of text, and can use this knowledge to provide accurate and informative responses to a wide range of questions. Additionally, Assistant is able to generate its own text based on the input it receives, 
        allowing it to engage in discussions and provide explanations and descriptions on a wide range of topics.

        This version of Assistant is called "Code Interpreter" and capable of using a python code interpreter (sandboxed jupyter kernel) to run code. 
        The human also maybe thinks this code interpreter is for writing code but it is more for data science, data analysis, and data visualization, file manipulation, and other things that can be done using a jupyter kernel/ipython runtime.
        Tell the human if they use the code interpreter incorrectly.
        Already installed packages are: (numpy pandas matplotlib seaborn scikit-learn yfinance scipy statsmodels sympy bokeh plotly dash networkx).
        If you encounter an error, try again and fix the code.
    """
    language = os.environ.get("OPENAI_LANGUAGE") or "chinese"
    cl.user_session.set(
        "message_history",
        [{
        "role":
        "system",
        "content": content + "\n\n" + "Please asnwer me in " + language
        }],
    )
    await cl.Avatar(
        name="Chatbot",
        url="https://avatars.githubusercontent.com/u/128686189?s=400&u=a1d1553023f8ea0921fba0debbe92a8c5f840dd9&v=4",
    ).send()


@cl.on_message
async def run_conversation(user_message: object):
  await on_message(user_message)


@cl.on_stop
async def stop_chat():
  global is_stop
  print("stop chat")
  is_stop = True
