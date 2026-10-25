
from fastapi import Header, HTTPException
from supabase_client import supabase


def verificar_propietario(
    parcela_id: str,
    authorization: str | None = Header(default=None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Inicia sesión para continuar")

    token = authorization[7:].strip()

    try:
        usuario = supabase.auth.get_user(token).user
    except Exception:
        raise HTTPException(401, "Sesión inválida o expirada")

    if not usuario:
        raise HTTPException(401, "Usuario no autenticado")

    parcela = (
        supabase.table("parcelas")
        .select("productor_id")
        .eq("id", parcela_id)
        .limit(1)
        .execute()
    )

    if not parcela.data or not parcela.data[0].get("productor_id"):
        raise HTTPException(404, "Parcela no disponible")

    propietario = (
        supabase.table("productores")
        .select("id")
        .eq("id", parcela.data[0]["productor_id"])
        .eq("auth_user_id", str(usuario.id))
        .limit(1)
        .execute()
    )

    if not propietario.data:
        raise HTTPException(404, "Parcela no disponible")

    return str(usuario.id)
