from fastapi import FastAPI, Depends, HTTPException, status
from pulumi import automation as auto
from loguru import logger
from datasync import execute_datasync_task
from ecs_service import start_ecs_service, stop_ecs_service
from pathlib import Path
from typing import Annotated
from update_hostname_ip import update_dynamic_dns
from datetime import datetime, timedelta, timezone
import jwt
from jwt.exceptions import InvalidTokenError

from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from pydantic import BaseModel
import json
import requests

from pwdlib import PasswordHash

import os

ACCESS_TOKEN_EXPIRE_MINUTES = 30


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str | None = None


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


class UserInDB(User):
    hashed_password: str


tetracubed_dir = Path("..", "tetracubed-core", "infrastructure")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

app = FastAPI()

def get_user(username: str):
    db = json.loads(os.getenv("USERS_DB"))
    if username in db:
        user_dict = db[username]
        return UserInDB(**user_dict)

def authenticate_user(username, password):
    users_db = json.loads(os.getenv("USERS_DB"))



    if username not in users_db.keys():
        return False

    password_hash = PasswordHash.recommended()

    if password_hash.verify(password, users_db[username]["hashed_password"]) is False:
        return False
    
    
    return UserInDB(**users_db[username])
    



def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, os.getenv("JWT_SECRET_KEY"), algorithm="HS256")
    return encoded_jwt


@app.post("/token")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return Token(access_token=access_token, token_type="bearer")

async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, os.getenv("JWT_SECRET_KEY"), algorithms=["HS256"])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except InvalidTokenError:
        raise credentials_exception
    user = get_user(username=token_data.username)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
):
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user



@app.get("/tetracubed/start")
async def tetracubed_start(current_user: str = Depends(get_current_user)):
    """
    Starts the Tetracubed Server
    """

    requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Provisioning Tetracubed Server...", "username": "Tetracubed-Fox"})
    try: 
        stack = auto.select_stack(
            stack_name=os.getenv("PULUMI_STACK_NAME"), work_dir=tetracubed_dir
        )

        stack.set_config("aws:region", auto.ConfigValue(value="eu-west-2"))
        stack.workspace.install_plugin("aws", "7.3.1")
        result = stack.up(on_output=print)
        print("Update summary:", result.summary.resource_changes)

        outputs = stack.outputs()
        logger.info(outputs)

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Loading World File & Plugin Config From S3 To EFS...", "username": "Tetracubed-Fox"})

        execute_datasync_task(outputs["s3_to_efs_task_arn"].value)


        logger.info(f"Spinning up task for service: {outputs['ecs_service_name'].value}")
        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Spinning Up ECS Compute Task...", "username": "Tetracubed-Fox"})
        public_ip = start_ecs_service(
            outputs["ecs_cluster_name"].value, outputs["ecs_service_name"].value
        )

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": f"Assigning Public IP: {public_ip} to tetranet.ddns.net", "username": "Tetracubed-Fox"})
        update_dynamic_dns(ecs_ip=public_ip)

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Tetracubed Has Been Successfully Provisioned!", "username": "Tetracubed-Fox"})
    except Exception as e:
         requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": f"Failed To Provision Exception: {e}", "username": "Tetracubed-Fox"})

    return {"message": "Background provisioning task was started"}

    



@app.get("/tetracubed/stop")
def tetracubed_stop(current_user: str = Depends(get_current_user)):
    """
    Stops the Tetracubed Server
    """

    requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Deprovisioning Tetracubed Server...", "username": "Tetracubed-Fox"})
    try: 
        stack = auto.select_stack(
            stack_name=os.getenv("PULUMI_STACK_NAME"), work_dir=tetracubed_dir
        )

        outputs = stack.outputs()

        logger.info(f"Spinning down task for service: {outputs['ecs_service_name'].value}")
        stop_ecs_service(
            outputs["ecs_cluster_name"].value, outputs["ecs_service_name"].value
        )

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Saving World File & Plugin Config From EFS To S3...", "username": "Tetracubed-Fox"})

        execute_datasync_task(outputs["efs_to_s3_task_arn"].value)

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Destroying Infrastructure...", "username": "Tetracubed-Fox"})

        result = stack.destroy(on_output=print)
        print("Update summary:", result.summary.resource_changes)

        requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": "Tetracubed Has Been Successfully Deprovisioned!", "username": "Tetracubed-Fox"})
    except Exception as e:
         requests.post(os.getenv("DISCORD_WEBHOOK_URL"), {"content": f"Failed To Deprovision Exception: {e}", "username": "Tetracubed-Fox"})


@app.get("/tetracubed/resources")
def show_resources(current_user: str = Depends(get_current_user)):
    """Shows All Tetracubed Resources"""
    pass
