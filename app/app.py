import json
import os
import traceback
import numpy as np
from openai import OpenAI
import boto3
import time
import uuid
import warnings
from datetime import datetime, timezone, timedelta
warnings.filterwarnings("ignore", category=FutureWarning)

API_OPENAI = os.environ['API_OPENAI']
VS_STUDENT = os.environ['VS_STUDENT']
VS_PERSONAL_PROFILES = json.loads(os.environ.get('VS_PERSONAL_PROFILES', '{}'))
MODEL = os.environ['MODEL']
MAX_NUM_RESULTS = int(os.environ['MAX_NUM_RESULTS'])
MAX_OUTPUT_TOKENS = int(os.environ['MAX_OUTPUT_TOKENS'])
MEMORY_MINUTES = int(os.environ['MEMORY_MINUTES'])
DEFAULT_MONTHLY_LIMIT = int(os.environ.get('DEFAULT_MONTHLY_LIMIT', '100'))
DEFAULT_PERSONAL_PROFILE_ID = os.environ.get('DEFAULT_PERSONAL_PROFILE_ID', '3_5')
PROMPT_FILE_STUDENT = os.environ['PROMPT_FILE_STUDENT']
PROMPT_FILE_PERSONAL = os.environ['PROMPT_FILE_PERSONAL']
POLICY_PROCESS_MAP_FILE = os.environ.get(
    'POLICY_PROCESS_MAP_FILE',
    'policy_process_map_context.txt'
)
REQUEST_TYPES = {'student', 'personal'}
UNKNOWN_GRADE = 'unknown'
LOG_PREVIEW_CHARS = int(os.environ.get('LOG_PREVIEW_CHARS', '800'))
REQUEST_CONFIG = {
    'student': {
        'prompt_file': PROMPT_FILE_STUDENT,
    },
    'personal': {
        'prompt_file': PROMPT_FILE_PERSONAL,
    },
}

dynamodb = boto3.client('dynamodb')
client_openai = OpenAI(api_key=API_OPENAI)


def preview_text(value, max_chars=LOG_PREVIEW_CHARS):
    text = str(value or '')
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def log_event(level, step, **data):
    payload = {
        'level': level,
        'step': step,
        'timestamp': get_gt_time(),
        **data,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))


def get_dynamo_value(item, key):
    value = item.get(key, {})
    if 'S' in value:
        return value['S']
    if 'N' in value:
        return value['N']
    if 'BOOL' in value:
        return value['BOOL']
    if 'NULL' in value:
        return None
    return None


def get_user_summary(user_data):
    if not user_data:
        return {}
    return {
        'user_id': get_dynamo_value(user_data, 'user_id'),
        'full_name': get_dynamo_value(user_data, 'full_name'),
        'user_type': get_dynamo_value(user_data, 'user_type'),
        'personal_profile_id': get_dynamo_value(user_data, 'personal_profile_id'),
        'grade': get_dynamo_value(user_data, 'grade'),
        'section': get_dynamo_value(user_data, 'section'),
        'monthly_limit': get_dynamo_value(user_data, 'monthly_limit'),
        'current_usage': get_dynamo_value(user_data, 'current_usage'),
        'reset_date': get_dynamo_value(user_data, 'reset_date'),
        'last_access_date': get_dynamo_value(user_data, 'last_access_date'),
    }


def normalize_request_type(request_type):
    return str(request_type or '').strip().lower()


def get_body_value(body, primary_key, fallback_key):
    value = body.get(primary_key)
    if value is None:
        value = body.get(fallback_key)
    return str(value or '').strip()


def get_user_grade(user_data):
    return (user_data or {}).get('grade', {}).get('S') or UNKNOWN_GRADE


def get_user_profile_id(user_data, request_type):
    if request_type != 'personal':
        return None
    return (user_data or {}).get('personal_profile_id', {}).get('S') or DEFAULT_PERSONAL_PROFILE_ID


def get_user_type(user_data):
    return user_data.get('user_type', {}).get('S')


def reconcile_user_access(user_id, user_data, requested_type):
    stored_type = get_user_type(user_data)

    if requested_type == 'personal':
        profile_id = user_data.get('personal_profile_id', {}).get('S') or DEFAULT_PERSONAL_PROFILE_ID
        if stored_type != 'personal' or 'personal_profile_id' not in user_data:
            response = dynamodb.update_item(
                TableName='Users',
                Key={'user_id': {'S': user_id}},
                UpdateExpression=(
                    'SET user_type = :personal, personal_profile_id = :profile_id '
                    'REMOVE grade, section'
                ),
                ExpressionAttributeValues={
                    ':personal': {'S': 'personal'},
                    ':profile_id': {'S': profile_id},
                },
                ReturnValues='ALL_NEW',
            )
            user_data = response['Attributes']
            log_event(
                'INFO',
                'user_promoted_to_personal',
                user_id=user_id,
                previous_user_type=stored_type,
                personal_profile_id=profile_id,
                requested_type=requested_type,
            )
        return user_data, 'personal'

    if stored_type == 'personal':
        if 'personal_profile_id' not in user_data:
            response = dynamodb.update_item(
                TableName='Users',
                Key={'user_id': {'S': user_id}},
                UpdateExpression='SET personal_profile_id = :profile_id',
                ExpressionAttributeValues={
                    ':profile_id': {'S': DEFAULT_PERSONAL_PROFILE_ID},
                },
                ReturnValues='ALL_NEW',
            )
            user_data = response['Attributes']
            log_event(
                'INFO',
                'personal_profile_defaulted',
                user_id=user_id,
                personal_profile_id=DEFAULT_PERSONAL_PROFILE_ID,
            )
        return user_data, 'personal'

    return user_data, 'student'


def get_vector_store_ids(request_type, profile_id=None):
    if request_type == 'student':
        return [VS_STUDENT]

    if request_type == 'personal':
        profile_vector_store_id = (
            VS_PERSONAL_PROFILES.get(profile_id or DEFAULT_PERSONAL_PROFILE_ID)
            or VS_PERSONAL_PROFILES.get(DEFAULT_PERSONAL_PROFILE_ID)
        )
        if not profile_vector_store_id:
            return []
        return [VS_STUDENT, profile_vector_store_id]

    return []


def get_gt_time():
        utc_time = datetime.now(timezone.utc)
        gt_time = utc_time - timedelta(hours=6)
        return gt_time.strftime("%Y-%m-%dT%H:%M:%S-06:00")


def cargar_prompt(nombre_archivo, carpeta="prompts"):
    ruta = os.path.join(carpeta, nombre_archivo)
    if not os.path.exists(ruta):
        ruta = os.path.join(os.path.dirname(__file__), carpeta, nombre_archivo)
    if not os.path.exists(ruta):
        raise FileNotFoundError(f"No se encontró el archivo de prompt: {ruta}")
    with open(ruta, "r", encoding="utf-8") as f:
        return f.read()


def load_policy_process_map(request_type):
    if request_type != 'personal':
        return ''
    try:
        return cargar_prompt(POLICY_PROCESS_MAP_FILE)
    except FileNotFoundError:
        log_event(
            'WARNING',
            'policy_process_map_missing',
            file=POLICY_PROCESS_MAP_FILE,
        )
        return ''


def verify_user(user_id):
    try:
        log_event('INFO', 'user_lookup_start', user_id=user_id)
        user_data = dynamodb.get_item(
            TableName='Users',
            Key={'user_id': {'S': user_id}}
        ).get('Item', {})

        if not user_data:
            log_event('INFO', 'user_lookup_new', user_id=user_id)
            return True, "Usuario nuevo", None

        monthly_limit = int(user_data.get('monthly_limit', {'N': '0'})['N'])
        current_usage = int(user_data.get('current_usage', {'N': '0'})['N'])
        reset_date = user_data.get('reset_date', {'S': ''})['S']

        current_month = get_gt_time()[:7]
        reset_month = reset_date[:7]
        log_event(
            'INFO',
            'user_lookup_found',
            user=get_user_summary(user_data),
            current_month=current_month,
            reset_month=reset_month,
        )

        if reset_month != current_month:
            new_reset_date = get_gt_time()
            dynamodb.update_item(
                TableName='Users',
                Key={'user_id': {'S': user_id}},
                UpdateExpression='SET current_usage = :zero, reset_date = :date',
                ExpressionAttributeValues={
                    ':zero': {'N': '0'},
                    ':date': {'S': new_reset_date}
                }
            )
            current_usage = 0
            log_event(
                'INFO',
                'usage_month_reset',
                user_id=user_id,
                previous_reset_date=reset_date,
                new_reset_date=new_reset_date,
            )

        remaining = monthly_limit - current_usage
        log_event(
            'INFO',
            'user_usage_checked',
            user_id=user_id,
            current_usage=current_usage,
            monthly_limit=monthly_limit,
            remaining=remaining,
        )

        if remaining <= 0:
            return False, f"Límite alcanzado de {monthly_limit} preguntas al mes", user_data

        return True, "Usuario verificado", user_data

    except Exception as e:
        log_event(
            'ERROR',
            'user_verify_error',
            user_id=user_id,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return False, "Error verificando usuario", {}


def create_user(user_id, request_type):
    try:
        current_date = get_gt_time()
        user_data = {
            'user_id': {'S': user_id},
            'full_name': {'S': user_id},
            'user_type': {'S': request_type},
            'monthly_limit': {'N': str(DEFAULT_MONTHLY_LIMIT)},
            'current_usage': {'N': '0'},
            'reset_date': {'S': current_date},
            'registration_date': {'S': current_date},
            'last_access_date': {'S': current_date},
        }

        if request_type == 'student':
            user_data['grade'] = {'NULL': True}
            user_data['section'] = {'NULL': True}
        elif request_type == 'personal':
            user_data['personal_profile_id'] = {'S': DEFAULT_PERSONAL_PROFILE_ID}

        dynamodb.put_item(
            TableName='Users',
            Item=user_data,
            ConditionExpression='attribute_not_exists(user_id)'
        )

        log_event(
            'INFO',
            'user_created',
            user=get_user_summary(user_data),
        )
        return user_data

    except dynamodb.exceptions.ConditionalCheckFailedException:
        user_data = dynamodb.get_item(
            TableName='Users',
            Key={'user_id': {'S': user_id}}
        ).get('Item', {})
        log_event(
            'INFO',
            'user_create_race_existing',
            user=get_user_summary(user_data),
        )
        return user_data
    except Exception as e:
        log_event(
            'ERROR',
            'user_create_error',
            user_id=user_id,
            request_type=request_type,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return {}


def update_usage(user_id):
    try:
        response = dynamodb.update_item(
            TableName='Users',
            Key={'user_id': {'S': user_id}},
            UpdateExpression='SET current_usage = current_usage + :inc, last_access_date = :date',
            ExpressionAttributeValues={
                ':inc': {'N': '1'},
                ':date': {'S': get_gt_time()}
            },
            ReturnValues='ALL_NEW'
        )

        current_usage = int(response['Attributes']['current_usage']['N'])
        monthly_limit = int(response['Attributes']['monthly_limit']['N'])
        remaining = monthly_limit - current_usage

        log_event(
            'INFO',
            'usage_updated',
            user_id=user_id,
            current_usage=current_usage,
            monthly_limit=monthly_limit,
            remaining=remaining,
        )

        return remaining

    except Exception as e:
        log_event(
            'ERROR',
            'usage_update_error',
            user_id=user_id,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return -1


def log_chat(data: dict):
    try:
        raw_id = uuid.uuid4().hex
        chat_id = f"chat_{raw_id}"
        chat_date = get_gt_time()

        item = {
            'chat_id': {'S': chat_id},
            'chat_date': {'S': chat_date},
        }

        for key, value in data.items():
            if isinstance(value, str):
                item[key] = {'S': value}
            elif isinstance(value, (int, float)):
                item[key] = {'N': str(value)}
            else:
                log_event('WARNING', 'chat_log_skipped_field', key=key, value_type=type(value).__name__)

        dynamodb.put_item(
            TableName='Chats',
            Item=item
        )

        log_event(
            'INFO',
            'chat_logged',
            chat_id=chat_id,
            user_id=data.get('user_id'),
            user_type=data.get('user_type'),
            status=data.get('status'),
            response_time=data.get('response_time'),
        )
        return True
    except Exception as e:
        log_event(
            'ERROR',
            'chat_log_error',
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return False


def get_last_response(user_id, request_type):
    try:
        current_time = get_gt_time()
        time_limit = datetime.strptime(current_time, "%Y-%m-%dT%H:%M:%S-06:00") - timedelta(minutes=MEMORY_MINUTES)
        time_limit_str = time_limit.strftime("%Y-%m-%dT%H:%M:%S-06:00")

        response = dynamodb.query(
            TableName='Chats',
            IndexName='UserChatsIndex',
            KeyConditionExpression='user_id = :user_id AND chat_date >= :time_limit',
            ExpressionAttributeValues={
                ':user_id': {'S': user_id},
                ':time_limit': {'S': time_limit_str}
            },
            ScanIndexForward=False,
            Limit=10
        )

        if response.get('Items'):
            for last_chat in response['Items']:
                chat_type = last_chat.get('user_type', {}).get('S', None)
                if chat_type == request_type:
                    last_response_id = last_chat.get('response_id', {}).get('S', None)
                    log_event(
                        'INFO',
                        'memory_match_found',
                        user_id=user_id,
                        request_type=request_type,
                        last_response_id=last_response_id,
                    )
                    return last_response_id
            log_event(
                'INFO',
                'memory_no_type_match',
                user_id=user_id,
                request_type=request_type,
                chats_checked=len(response.get('Items', [])),
            )
            return None
        log_event('INFO', 'memory_empty', user_id=user_id, request_type=request_type)
        return None
    except Exception as e:
        log_event(
            'ERROR',
            'memory_lookup_error',
            user_id=user_id,
            request_type=request_type,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return None

def generate_response(user_id, question, request_type, grade, profile_id=None):
    try:
        if MEMORY_MINUTES>0:
            last_response_id = get_last_response(user_id, request_type)
        else:
            last_response_id = None

        request_config = REQUEST_CONFIG.get(request_type)
        if request_config is None:
            raise Exception(f"No existe configuración para el tipo: {request_type}")

        vector_store_ids = get_vector_store_ids(request_type, profile_id)
        if not vector_store_ids:
            raise Exception(f"No existe un vector store para el tipo: {request_type}")

        prompt_file = request_config['prompt_file']
        if not prompt_file:
            raise Exception(f"No existe un prompt para el tipo: {request_type}")

        prompt_template = cargar_prompt(prompt_file)
        policy_process_map = load_policy_process_map(request_type)

        instructions = prompt_template.format(
            user_id=user_id,
            request_type=request_type,
            grade=grade,
            profile_id=profile_id or '',
            policy_process_map=policy_process_map
        )

        log_event(
            'INFO',
            'openai_request_start',
            user_id=user_id,
            request_type=request_type,
            grade=grade,
            profile_id=profile_id,
            model=MODEL,
            prompt_file=prompt_file,
            vector_store_ids=vector_store_ids,
            max_num_results=MAX_NUM_RESULTS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            memory_minutes=MEMORY_MINUTES,
            has_previous_response=bool(last_response_id),
            policy_process_map_chars=len(policy_process_map),
            question_preview=preview_text(question),
        )

        response = client_openai.responses.create(
            model=MODEL,
            tools=[{
                "type": 'file_search',
                "vector_store_ids": vector_store_ids,
                "max_num_results": MAX_NUM_RESULTS,
            }, {
                "type": 'web_search',
            }],
            tool_choice="auto",
            reasoning={"effort": "low"},
            instructions=instructions,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            input=question,
            previous_response_id=last_response_id
        )

        answer = response.output_text
        response_id = response.id
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        output_types = {
            getattr(output_item, 'type', None)
            for output_item in (response.output or [])
        }
        used_web_search = 'web_search_call' in output_types
        used_file_search = 'file_search_call' in output_types
        if used_web_search and used_file_search:
            search_type = 'web_search_and_file_search'
        elif used_web_search:
            search_type = 'web_search'
        elif used_file_search:
            search_type = 'file_search'
        else:
            search_type = 'none'

        log_event(
            'INFO',
            'openai_response_received',
            user_id=user_id,
            request_type=request_type,
            profile_id=profile_id,
            response_id=response_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            question_preview=preview_text(question),
            answer_preview=preview_text(answer),
            answer_chars=len(answer or ''),
            search_type=search_type,
            used_web_search=used_web_search,
            used_file_search=used_file_search,
        )

        return True, answer, response_id, input_tokens, output_tokens, search_type
    except Exception as e:
        log_event(
            'ERROR',
            'openai_response_error',
            user_id=user_id,
            request_type=request_type,
            grade=grade,
            profile_id=profile_id,
            question_preview=preview_text(question),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return False, "Error generando respuesta", None, 0, 0, 'none'


def route_chat_request(user_id, question, request_type, grade, profile_id=None):
    routes = {
        'student': generate_response,
        'personal': generate_response,
    }

    handler = routes.get(request_type)
    if handler is None:
        return False, "Tipo de usuario inválido", None, 0, 0, 'none'

    log_event(
        'INFO',
        'route_selected',
        user_id=user_id,
        request_type=request_type,
        grade=grade,
        profile_id=profile_id,
    )
    return handler(user_id, question, request_type, grade, profile_id)


def lambda_handler(event, context):
    try:
        request_context = event.get('requestContext', {})
        http_info = request_context.get('http', {})

        method = http_info.get('method', '')
        path = http_info.get('path', '')

        request_id = getattr(context, 'aws_request_id', None)

        log_event(
            'INFO',
            'request_received',
            request_id=request_id,
            method=method,
            path=path,
        )

        if method == 'POST' and path == '/chatbot':
            start_time = time.time()

            if not event.get('body'):
                log_event('WARNING', 'request_missing_body', request_id=request_id)
                return {'statusCode': 200, 'body': json.dumps({'respuesta': '[ERROR] Body requerido'})}

            try:
                body = json.loads(event['body'])
            except json.JSONDecodeError:
                log_event('WARNING', 'request_invalid_json', request_id=request_id)
                return {'statusCode': 200, 'body': json.dumps({'respuesta': '[ERROR] JSON inválido'})}

            if not isinstance(body, dict):
                return {'statusCode': 400, 'body': json.dumps({'respuesta': '[ERROR] El JSON debe ser un objeto'})}

            user_id = get_body_value(body, 'user', 'usuario')
            question = get_body_value(body, 'question', 'pregunta')
            requested_type = normalize_request_type(body.get('type') or body.get('tipo'))

            log_event(
                'INFO',
                'request_parsed',
                request_id=request_id,
                user_id=user_id,
                requested_type=requested_type,
                question_preview=preview_text(question),
                body_keys=sorted(body.keys()),
            )

            if not user_id or not question or not requested_type:
                log_event(
                    'WARNING',
                    'request_validation_error',
                    request_id=request_id,
                    user_id=user_id,
                    requested_type=requested_type,
                    has_question=bool(question),
                )
                return {
                    'statusCode': 200,
                    'body': json.dumps({'respuesta': '[ERROR] Se requieren "user", "question" y "type"'})
                }

            if requested_type not in REQUEST_TYPES:
                log_event(
                    'WARNING',
                    'request_invalid_type',
                    request_id=request_id,
                    user_id=user_id,
                    requested_type=requested_type,
                )
                return {
                    'statusCode': 200,
                    'body': json.dumps({'respuesta': '[ERROR] "tipo" debe ser "student" o "personal"'})
                }


            # 1. VERIFICACION DE USUARIO
            log_event('INFO', 'step_verify_user_start', request_id=request_id, user_id=user_id)
            user_ok, message, user_data = verify_user(user_id)
            if user_ok and user_data is None:
                log_event(
                    'INFO',
                    'step_create_user_start',
                    request_id=request_id,
                    user_id=user_id,
                    requested_type=requested_type,
                )
                user_data = create_user(user_id, requested_type)
                if not user_data:
                    user_ok = False
                    message = "Error registrando usuario"

            effective_type = requested_type
            if user_ok and user_data:
                user_data, effective_type = reconcile_user_access(user_id, user_data, requested_type)

            grade = get_user_grade(user_data)
            profile_id = get_user_profile_id(user_data, effective_type)
            log_event(
                'INFO',
                'step_verify_user_done',
                request_id=request_id,
                user_ok=user_ok,
                message=message,
                user=get_user_summary(user_data),
                requested_type=requested_type,
                effective_type=effective_type,
                grade=grade,
                profile_id=profile_id,
            )
            if not user_ok:
                response_time = round((time.time() - start_time), 4)
                log_chat({
                        'user_id': user_id,
                        'user_type': effective_type,
                        'requested_type': requested_type,
                        'grade': grade,
                        'personal_profile_id': profile_id or '',
                        'question': question,
                        'answer': message,
                        'status': 'rejected',
                        'response_time': response_time
                        })
                log_event(
                    'INFO',
                    'request_rejected',
                    request_id=request_id,
                    user_id=user_id,
                    requested_type=requested_type,
                    effective_type=effective_type,
                    message=message,
                    response_time=response_time,
                )
                return {
                    'statusCode': 200,
                    'body': json.dumps({'respuesta': message})
                }

            # 2. GENERAR RESPUESTA
            log_event(
                'INFO',
                'step_generate_response_start',
                request_id=request_id,
                user_id=user_id,
                requested_type=requested_type,
                effective_type=effective_type,
                grade=grade,
                profile_id=profile_id,
            )
            ok_response, answer, response_id, input_tokens, output_tokens, search_type = route_chat_request(user_id, question, effective_type, grade, profile_id)
            if not ok_response:
                response_time = round((time.time() - start_time), 4)
                log_chat({
                        'user_id': user_id,
                        'user_type': effective_type,
                        'requested_type': requested_type,
                        'grade': grade,
                        'personal_profile_id': profile_id or '',
                        'question': question,
                        'answer': answer,
                        'status': 'error',
                        'search_type': search_type,
                        'response_time': response_time
                        })
                log_event(
                    'ERROR',
                    'request_generation_failed',
                    request_id=request_id,
                    user_id=user_id,
                    requested_type=requested_type,
                    effective_type=effective_type,
                    grade=grade,
                    profile_id=profile_id,
                    question_preview=preview_text(question),
                    answer_preview=preview_text(answer),
                    response_time=response_time,
                )
                return {
                    'statusCode': 200,
                    'body': json.dumps({'respuesta': answer})
                }

            # 3. REGISTRAR CHAT
            log_event(
                'INFO',
                'step_update_usage_start',
                request_id=request_id,
                user_id=user_id,
                requested_type=requested_type,
                effective_type=effective_type,
            )
            remaining = update_usage(user_id)
            response_time = round((time.time() - start_time), 4)
            log_chat({
                    'user_id': user_id,
                    'user_type': effective_type,
                    'requested_type': requested_type,
                    'grade': grade,
                    'personal_profile_id': profile_id or '',
                    'question': question,
                    'answer': answer,
                    'status': 'completed',
                    'response_time': response_time,
                    'response_id': response_id,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'search_type': search_type,
                    })

            threshold = 20
            if remaining is not None and remaining < threshold:
                message = f"{answer}\n**Preguntas restantes:** {remaining}"
            else:
                message = answer
            log_event(
                'INFO',
                'request_completed',
                request_id=request_id,
                user_id=user_id,
                requested_type=requested_type,
                effective_type=effective_type,
                grade=grade,
                profile_id=profile_id,
                status='completed',
                response_id=response_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                search_type=search_type,
                remaining=remaining,
                response_time=response_time,
                question_preview=preview_text(question),
                answer_preview=preview_text(answer),
            )
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'respuesta': message
                })
            }

        elif method == 'HEAD' and path == '/health':
            log_event('INFO', 'health_ok', request_id=request_id)
            return {
                'statusCode': 200,
                'body': ''
            }
        else:
            log_event(
                'WARNING',
                'route_not_found',
                request_id=request_id,
                method=method,
                path=path,
            )
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'respuesta': '[ERROR] Ruta no encontrada'
                })
            }
    except Exception as e:
        traceback_str = traceback.format_exc()
        log_event(
            'ERROR',
            'unhandled_exception',
            error=str(e),
            traceback=traceback_str,
        )

        return {
            'statusCode': 500,
            'body': json.dumps({'respuesta': '[ERROR] Error interno del servidor'})
        }
