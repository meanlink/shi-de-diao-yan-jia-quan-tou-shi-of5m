from pydantic import BaseModel, Field, StrictInt
class JobInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    params: dict = Field(default_factory=dict)
class CapacityInput(BaseModel):
    # StrictInt: reject booleans/strings/floats instead of silently coercing them.
    capacity: StrictInt
