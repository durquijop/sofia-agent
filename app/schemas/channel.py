from pydantic import BaseModel, Field


class ChannelMedia(BaseModel):
    kind: str
    url: str | None = None
    caption: str | None = None
    filename: str | None = None
    raw: dict | None = None


class ChannelInboundMessage(BaseModel):
    channel: str
    provider: str | None = None
    sender_id: str
    sender_phone: str | None = None
    sender_name: str | None = None
    external_conversation_id: str
    external_message_id: str
    channel_account_id: str | None = None
    message_type: str = "text"
    text: str | None = None
    timestamp: str
    has_media: bool = False
    media: list[ChannelMedia] = Field(default_factory=list)
    raw_payload: dict | None = None


class ChannelOutboundMessage(BaseModel):
    channel: str
    provider: str | None = None
    recipient_id: str
    recipient_phone: str | None = None
    channel_account_id: str | None = None
    external_message_id: str | None = None
    external_conversation_id: str | None = None
    reply_type: str = "text"
    text: str | None = None
    suppress_send: bool = False
    metadata: dict | None = None
