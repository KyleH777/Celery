from pydantic import BaseModel, Field


class CompanyAnalysis(BaseModel):
    value_proposition: str = Field(
        ...,
        description="The company's core value proposition in one or two sentences.",
    )
    target_audience: str = Field(
        ...,
        description="The primary customer segment the company sells to.",
    )
    pain_points: list[str] = Field(
        ...,
        min_length=2,
        max_length=3,
        description="2-3 likely business pain points this company faces.",
    )


class FinalOutreach(BaseModel):
    analysis: CompanyAnalysis
    personalized_pitch: str = Field(
        ...,
        description=(
            "A cold outreach message following the Problem-Agitate-Solve framework, "
            "referencing at least one specific detail from the company's website."
        ),
    )
