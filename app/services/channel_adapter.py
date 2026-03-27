from app.schemas.channel import ChannelInboundMessage, ChannelMedia
from app.schemas.kapso import KapsoInboundRequest


def normalize_kapso_inbound(request: KapsoInboundRequest) -> ChannelInboundMessage:
    media_items: list[ChannelMedia] = []
    if request.has_media:
        media_items.append(
            ChannelMedia(
                kind=str(request.message_type or "unknown"),
                raw=request.media_raw if isinstance(request.media_raw, dict) else None,
            )
        )

    return ChannelInboundMessage(
        channel="whatsapp",
        provider="kapso",
        sender_id=request.from_phone,
        sender_phone=request.from_phone,
        sender_name=request.contact_name,
        external_conversation_id=request.kapso_conversation_id,
        external_message_id=request.message_id,
        channel_account_id=request.phone_number_id,
        message_type=str(request.message_type or "text"),
        text=request.text,
        timestamp=request.timestamp,
        has_media=bool(request.has_media),
        media=media_items,
        raw_payload={
            "phone_number_id": request.phone_number_id,
            "media_raw": request.media_raw,
        },
    )
