from fastapi import APIRouter, Request

from .assistant import answer_question
from .db import connection
from .models import AssistantQuestion


router = APIRouter()


@router.post("/people/{person_id}/assistant")
def ask_about_person(
    person_id: int,
    payload: AssistantQuestion,
    request: Request,
):
    with connection() as conn:
        return answer_question(
            conn,
            request.state.principal,
            person_id,
            payload.question,
        )
