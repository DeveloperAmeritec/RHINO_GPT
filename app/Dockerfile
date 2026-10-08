# Imagen base para Lambda con Python 3.12
FROM public.ecr.aws/lambda/python:3.12

# Instalar dependencias del sistema en una sola capa para mejorar el caché
RUN microdnf install -y gcc-c++ python3-devel libgomp && microdnf clean all

# Copiar el archivo de requirements e instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código y recursos
COPY *.py ./
COPY prompts/ ./prompts/

# Comando de Lambda
CMD ["app.lambda_handler"]
