import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from llm_client import LLMClient
from llm_client import is_thinking_token, strip_thinking_prefix, TokenUsage

CONVERSATIONS_DIR = Path(__file__).parent / "conversations"

SYSTEM_PROMPT_TEMPLATES = {
    "zh": """你是一位严谨的学者和工程师，专精于数学、物理学和计算机科学（包括编程、算法、系统设计）。你正在与另一位AI模型就以下问题进行学术讨论：

{topic}

讨论规则：
1. 根据问题类型选择合适的分析方式：数学/物理问题用公式推导，代码/工程问题给出实现代码和分析
2. 数学公式使用 LaTeX 格式（行内用 $...$，独立公式用 $$...$$）；代码使用 Markdown 代码块（标注语言）
3. 审查对方模型的回答，指出其中的错误、漏洞或不足
4. 如果你完全同意对方的推导、结论或实现方案，请在回复的**最开头**写上 [AGREE]，然后简要说明你同意的理由
5. 如果你不同意，请详细说明你不同意的原因，并给出正确的推导或实现
6. 保持严谨性，不要为了迎合对方而妥协你的判断
""",
    "en": """You are a rigorous scholar and engineer specializing in mathematics, physics, and computer science (including programming, algorithms, and system design). You are engaged in an academic discussion with another AI model on the following topic:

{topic}

Discussion rules:
1. Choose an appropriate analysis method based on the problem type: formula derivation for math/physics, code implementation for engineering
2. Use LaTeX for math formulas (inline $...$, display $$...$$); use Markdown code blocks (with language tags)
3. Review the other model's response, pointing out errors, gaps, or shortcomings
4. If you fully agree with the other model's derivation, conclusion, or implementation, start your response with **[AGREE]** and briefly explain why
5. If you disagree, explain your reasons in detail and provide the correct derivation or implementation
6. Maintain rigor — do not compromise your judgment to accommodate the other model
""",
}

BLIND_SYSTEM_PROMPT_TEMPLATES = {
    "zh": """你是一位严谨的学者和工程师，专精于数学、物理学和计算机科学（包括编程、算法、系统设计）。你正在独立解决以下问题：

{topic}

独立解答规则：
1. 根据问题类型选择合适的分析方式：数学/物理问题用公式推导，代码/工程问题给出实现代码和分析
2. 数学公式使用 LaTeX 格式（行内用 $...$，独立公式用 $$...$$）；代码使用 Markdown 代码块（标注语言）
3. 给出你完整、严谨的推导或实现，以及最终结论
4. 如果你完全同意对方模型的答案和推导，请在回复的**最开头**写上 [AGREE]
5. 保持严谨性，不要为了迎合对方而妥协你的判断
""",
    "en": """You are a rigorous scholar and engineer specializing in mathematics, physics, and computer science (including programming, algorithms, and system design). You are independently solving the following problem:

{topic}

Rules:
1. Choose an appropriate analysis method: formula derivation for math/physics, code implementation for engineering
2. Use LaTeX for math formulas (inline $...$, display $$...$$); use Markdown code blocks (with language tags)
3. Provide a complete, rigorous derivation or implementation, along with your final conclusion
4. If you fully agree with the other model's answer and derivation, start your response with **[AGREE]**
5. Maintain rigor — do not compromise your judgment
""",
}


class DebateSession:
    def __init__(
        self,
        topic: str,
        model_a_key: str,
        model_b_key: str,
        model_a: LLMClient,
        model_b: LLMClient,
        name_a: str,
        name_b: str,
        max_rounds: int = 20,
        disagreement_threshold: int = 5,
        mode: str = "sequential",
        model_c: LLMClient = None,
        model_c_key: str = "",
        name_c: str = "",
        memory_masking: bool = False,
        masking_model: LLMClient = None,
        diversity_retention: bool = False,
        embedding_url: str = "",
        embedding_key: str = "",
        embedding_model: str = "",
    ):
        self.id = uuid.uuid4().hex[:8]
        self.topic = topic
        self.model_a = model_a
        self.model_b = model_b
        self.model_a_key = model_a_key
        self.model_b_key = model_b_key
        self.name_a = name_a
        self.name_b = name_b
        self.max_rounds = max_rounds
        self.disagreement_threshold = disagreement_threshold
        self.mode = mode  # "sequential", "blind", or "chain3"

        # 3-model chain debate
        self.model_c = model_c
        self.model_c_key = model_c_key
        self.name_c = name_c

        # Memory masking
        self.memory_masking = memory_masking
        self.masking_model = masking_model

        # Diversity retention
        self.diversity_retention = diversity_retention
        self.embedding_url = embedding_url
        self.embedding_key = embedding_key
        self.embedding_model = embedding_model

        self.lang = "zh"
        sys_prompt = self._get_system_prompt(mode)

        self.messages_a: list[dict] = [{"role": "system", "content": sys_prompt}]
        self.messages_b: list[dict] = [{"role": "system", "content": sys_prompt}]
        if mode == "chain3" and model_c:
            self.messages_c: list[dict] = [{"role": "system", "content": sys_prompt}]
        else:
            self.messages_c = []

        self.history: list[dict] = []
        self.round = 0
        self.status = "idle"
        self.status_detail = ""
        self.consecutive_disagreements = 0
        self.error_message = ""
        self.report = ""
        self.report_model = ""
        self.quality_score = ""
        self.created_at = datetime.now().isoformat()
        self.token_usage = {}  # model_name -> {"prompt": int, "completion": int, "thinking": int, "total": int, "calls": int}
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._stop_flag = False
        self.owner_id: int | None = None

    def _get_system_prompt(self, mode: str) -> str:
        if mode == "blind":
            return BLIND_SYSTEM_PROMPT_TEMPLATES.get(self.lang, BLIND_SYSTEM_PROMPT_TEMPLATES["zh"]).format(topic=self.topic)
        return SYSTEM_PROMPT_TEMPLATES.get(self.lang, SYSTEM_PROMPT_TEMPLATES["zh"]).format(topic=self.topic)

    def set_lang(self, lang: str):
        self.lang = lang
        sys_prompt = self._get_system_prompt(self.mode)
        self.messages_a[0] = {"role": "system", "content": sys_prompt}
        self.messages_b[0] = {"role": "system", "content": sys_prompt}
        if self.mode == "chain3" and self.messages_c:
            self.messages_c[0] = {"role": "system", "content": sys_prompt}

    def _emit(self, event_type: str, data: dict):
        try:
            self._event_queue.put_nowait(_DebateEvent(event_type, data))
        except asyncio.QueueFull:
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._event_queue.put_nowait(_DebateEvent(event_type, data))
            except asyncio.QueueFull:
                pass

    def _check_agreement(self, text: str) -> bool:
        return text.strip().upper().startswith("[AGREE]")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "topic": self.topic,
            "model_a_key": self.model_a_key, "model_b_key": self.model_b_key,
            "name_a": self.name_a, "name_b": self.name_b,
            "max_rounds": self.max_rounds, "status": self.status,
            "status_detail": self.status_detail, "round": self.round,
            "mode": self.mode,
            "model_c_key": self.model_c_key, "name_c": self.name_c,
            "memory_masking": self.memory_masking,
            "diversity_retention": self.diversity_retention,
            "created_at": self.created_at,
            "report": self.report, "report_model": self.report_model,
            "error_message": self.error_message,
            "history": self.history,
            "token_usage": self.token_usage,
            "owner_id": self.owner_id,
            "quality_score": self.quality_score,
        }

    def save_to_disk(self):
        CONVERSATIONS_DIR.mkdir(exist_ok=True)
        path = CONVERSATIONS_DIR / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self.save_markdown()

    @staticmethod
    def load_from_disk(filepath: Path) -> "DebateSession":
        data = json.loads(filepath.read_text(encoding="utf-8"))
        s = DebateSession.__new__(DebateSession)
        s.id = data["id"]
        s.topic = data["topic"]
        s.model_a = s.model_b = s.model_c = None
        s.model_a_key = data.get("model_a_key", "")
        s.model_b_key = data.get("model_b_key", "")
        s.model_c_key = data.get("model_c_key", "")
        s.name_a = data.get("name_a", "")
        s.name_b = data.get("name_b", "")
        s.name_c = data.get("name_c", "")
        s.max_rounds = data.get("max_rounds", 20)
        s.mode = data.get("mode", "sequential")
        s.status = data.get("status", "idle")
        s.status_detail = data.get("status_detail", "")
        s.round = data.get("round", 0)
        s.consecutive_disagreements = 0
        s.error_message = data.get("error_message", "")
        s.report = data.get("report", "")
        s.report_model = data.get("report_model", "")
        s.created_at = data.get("created_at", "")
        s.history = data.get("history", [])
        s.token_usage = data.get("token_usage", {})
        s.messages_a = []
        s.messages_b = []
        s.messages_c = []
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = False
        s.memory_masking = data.get("memory_masking", False)
        s.masking_model = None
        s.diversity_retention = data.get("diversity_retention", False)
        s.embedding_url = data.get("embedding_url", "")
        s.embedding_key = data.get("embedding_key", "")
        s.embedding_model = data.get("embedding_model", "")
        s.lang = "zh"
        s.owner_id = data.get("owner_id")
        s.quality_score = data.get("quality_score", "")
        return s

    async def run(self):
        self._stop_flag = False
        if self.mode == "blind":
            await self.run_blind()
        elif self.mode == "chain3":
            await self.run_chain3()
        else:
            await self.run_sequential()

    async def run_sequential(self):
        self.status = "running"
        self.status_detail = ""
        try:
            self.messages_a.append(
                {"role": "user", "content": f"请分析以下问题并给出你的推导：\n\n{self.topic}"}
            )
            self.round = 1
            self.status_detail = f"{self.name_a} 思考中..."
            self._emit("round_start", {"round": self.round, "model": self.name_a})
            response_a = await self._stream_model(
                self.model_a, self.messages_a, self.name_a, self.round
            )
            if self._stop_flag:
                self._finish_stopped(); return

            self.messages_a.append({"role": "assistant", "content": response_a})
            self.history.append({"round": self.round, "model": self.name_a, "content": response_a})
            self._emit("round_end", {"round": self.round, "model": self.name_a, "agreed": self._check_agreement(response_a)})

            turn = "b"
            while self.round < self.max_rounds and not self._stop_flag:
                self.round += 1
                if turn == "b":
                    model_client, messages, model_name = self.model_b, self.messages_b, self.name_b
                else:
                    model_client, messages, model_name = self.model_a, self.messages_a, self.name_a

                last_response = self.history[-1]
                prompt = (
                    f"对方模型（{last_response['model']}）的回复如下：\n\n"
                    f"---\n{last_response['content']}\n---\n\n"
                    f"请回应上述观点。"
                )
                if self.memory_masking and self.round > 3:
                    masked = await self._apply_memory_masking(messages)
                    if len(masked) < len(messages):
                        messages.clear()
                        messages.extend(masked)
                messages.append({"role": "user", "content": prompt})
                self.status_detail = f"{model_name} 思考中..."
                self._emit("round_start", {"round": self.round, "model": model_name})
                response = await self._stream_model(model_client, messages, model_name, self.round)
                if self._stop_flag:
                    self._finish_stopped(); return

                # Diversity retention check
                if self.diversity_retention:
                    if not await self._is_diverse_enough(response):
                        self._emit("round_end", {"round": self.round, "model": model_name, "agreed": False, "filtered": True})
                        turn = "a" if turn == "b" else "b"
                        continue

                messages.append({"role": "assistant", "content": response})
                self.history.append({"round": self.round, "model": model_name, "content": response})
                agreed = self._check_agreement(response)
                self._emit("round_end", {"round": self.round, "model": model_name, "agreed": agreed})

                if agreed and len(self.history) >= 2 and self._check_agreement(self.history[-2]["content"]):
                    self._finish("consensus", "达成共识"); return

                self.consecutive_disagreements = self.consecutive_disagreements + 1 if not agreed else 0
                if self.round >= 10 and self.consecutive_disagreements >= self.disagreement_threshold:
                    self._finish("disagreement", "存在分歧"); return

                turn = "a" if turn == "b" else "b"

            if not self._stop_flag:
                self.status = "max_rounds"
                self.status_detail = "正在生成报告..."
                self._emit("debate_end", {"status": "max_rounds", "rounds": self.round})
                await self._generate_report()
                self.save_to_disk()

        except Exception as e:
            self._finish_error(e)

    async def run_blind(self):
        self.status = "running"
        self.status_detail = ""
        try:
            # Round 1: both answer independently
            self.round = 1
            self.messages_a.append(
                {"role": "user", "content": f"请独立分析以下问题并给出你的推导：\n\n{self.topic}"}
            )
            self.messages_b.append(
                {"role": "user", "content": f"请独立分析以下问题并给出你的推导：\n\n{self.topic}"}
            )
            self.status_detail = f"第 1 轮 — 双方独立思考中..."
            self._emit("round_start", {"round": 1, "model": f"{self.name_a} & {self.name_b}"})

            response_a, response_b = await asyncio.gather(
                self._stream_model(self.model_a, self.messages_a, self.name_a, 1),
                self._stream_model(self.model_b, self.messages_b, self.name_b, 1),
            )
            if self._stop_flag:
                self._finish_stopped(); return

            self.messages_a.append({"role": "assistant", "content": response_a})
            self.messages_b.append({"role": "assistant", "content": response_b})
            self.history.append({"round": 1, "model": self.name_a, "content": response_a})
            self.history.append({"round": 1, "model": self.name_b, "content": response_b})
            self._emit("round_end", {"round": 1, "model": self.name_a, "agreed": False})
            self._emit("round_end", {"round": 1, "model": self.name_b, "agreed": False})

            if self._check_agreement(response_a) and self._check_agreement(response_b):
                self._finish("consensus", "达成共识"); return

            # Rounds 2+: exchange latest answers
            latest_a, latest_b = response_a, response_b
            while self.round < self.max_rounds and not self._stop_flag:
                self.round += 1

                # Memory masking
                if self.memory_masking and self.round > 3:
                    self.messages_a = await self._apply_memory_masking(self.messages_a)
                    self.messages_b = await self._apply_memory_masking(self.messages_b)

                self.messages_a.append({
                    "role": "user",
                    "content": (
                        f"另一个模型（{self.name_b}）对同一问题的最新回答如下：\n\n"
                        f"---\n{latest_b}\n---\n\n"
                        f"请参考对方观点，给出你更新后的推导和最终答案。"
                        f"如果你完全同意对方的答案，请在回复最开头写上 [AGREE]。"
                    ),
                })
                self.messages_b.append({
                    "role": "user",
                    "content": (
                        f"另一个模型（{self.name_a}）对同一问题的最新回答如下：\n\n"
                        f"---\n{latest_a}\n---\n\n"
                        f"请参考对方观点，给出你更新后的推导和最终答案。"
                        f"如果你完全同意对方的答案，请在回复最开头写上 [AGREE]。"
                    ),
                })

                self.status_detail = f"第 {self.round} 轮 — 双方交换观点中..."
                self._emit("round_start", {"round": self.round, "model": f"{self.name_a} & {self.name_b}"})

                response_a, response_b = await asyncio.gather(
                    self._stream_model(self.model_a, self.messages_a, self.name_a, self.round),
                    self._stream_model(self.model_b, self.messages_b, self.name_b, self.round),
                )
                if self._stop_flag:
                    self._finish_stopped(); return

                self.messages_a.append({"role": "assistant", "content": response_a})
                self.messages_b.append({"role": "assistant", "content": response_b})

                # Diversity retention
                add_a = True
                add_b = True
                if self.diversity_retention:
                    add_a = await self._is_diverse_enough(response_a)
                    add_b = await self._is_diverse_enough(response_b)

                if add_a:
                    self.history.append({"round": self.round, "model": self.name_a, "content": response_a})
                if add_b:
                    self.history.append({"round": self.round, "model": self.name_b, "content": response_b})

                agreed_a = self._check_agreement(response_a)
                agreed_b = self._check_agreement(response_b)
                self._emit("round_end", {"round": self.round, "model": self.name_a, "agreed": agreed_a, "filtered": not add_a})
                self._emit("round_end", {"round": self.round, "model": self.name_b, "agreed": agreed_b, "filtered": not add_b})

                if agreed_a and agreed_b:
                    self._finish("consensus", "达成共识"); return

                latest_a, latest_b = response_a, response_b

            if not self._stop_flag:
                self.status = "max_rounds"
                self.status_detail = "正在生成报告..."
                self._emit("debate_end", {"status": "max_rounds", "rounds": self.round})
                await self._generate_report()
                self.save_to_disk()

        except Exception as e:
            self._finish_error(e)

    async def run_chain3(self):
        """3-model chain debate: A -> B -> C -> A -> ..."""
        self.status = "running"
        self.status_detail = ""
        try:
            # Round 1: all three answer independently in parallel
            self.round = 1
            self.messages_a.append({"role": "user", "content": f"请独立分析以下问题并给出你的推导：\n\n{self.topic}"})
            self.messages_b.append({"role": "user", "content": f"请独立分析以下问题并给出你的推导：\n\n{self.topic}"})
            self.messages_c.append({"role": "user", "content": f"请独立分析以下问题并给出你的推导：\n\n{self.topic}"})

            self.status_detail = f"第 1 轮 — 三方独立思考中..."
            self._emit("round_start", {"round": 1, "model": f"{self.name_a} & {self.name_b} & {self.name_c}"})

            response_a, response_b, response_c = await asyncio.gather(
                self._stream_model(self.model_a, self.messages_a, self.name_a, 1),
                self._stream_model(self.model_b, self.messages_b, self.name_b, 1),
                self._stream_model(self.model_c, self.messages_c, self.name_c, 1),
            )
            if self._stop_flag:
                self._finish_stopped(); return

            self.messages_a.append({"role": "assistant", "content": response_a})
            self.messages_b.append({"role": "assistant", "content": response_b})
            self.messages_c.append({"role": "assistant", "content": response_c})
            self.history.append({"round": 1, "model": self.name_a, "content": response_a})
            self.history.append({"round": 1, "model": self.name_b, "content": response_b})
            self.history.append({"round": 1, "model": self.name_c, "content": response_c})
            self._emit("round_end", {"round": 1, "model": self.name_a, "agreed": False})
            self._emit("round_end", {"round": 1, "model": self.name_b, "agreed": False})
            self._emit("round_end", {"round": 1, "model": self.name_c, "agreed": False})

            all_agree = all(self._check_agreement(r) for r in [response_a, response_b, response_c])
            if all_agree:
                self._finish("consensus", "三方达成共识"); return

            # Chain: rotate through A -> B -> C -> A ...
            models = [
                (self.name_a, self.model_a, self.messages_a),
                (self.name_b, self.model_b, self.messages_b),
                (self.name_c, self.model_c, self.messages_c),
            ]
            latest_responses = {self.name_a: response_a, self.name_b: response_b, self.name_c: response_c}
            turn_idx = 0  # start with A responding to C

            while self.round < self.max_rounds and not self._stop_flag:
                self.round += 1
                # Current model responds to the PREVIOUS model's latest response
                prev_idx = (turn_idx + 2) % 3
                prev_name, _, prev_messages = models[prev_idx]
                curr_name, curr_client, curr_messages = models[turn_idx]

                # Apply memory masking to current model's message history
                if self.memory_masking and self.round > 3:
                    masked = await self._apply_memory_masking(curr_messages)
                    if len(masked) < len(curr_messages):
                        curr_messages.clear()
                        curr_messages.extend(masked)

                # Build prompt from previous model's latest response
                prev_text = latest_responses[prev_name]
                if self.memory_masking and self.masking_model and self.round > 3:
                    try:
                        prev_text = await self.masking_model.chat([{
                            "role": "user",
                            "content": f"请提取以下回复的关键论点，去除错误推导，输出精简摘要（不超过300字）：\n\n{prev_text}"
                        }])
                    except Exception:
                        prev_text = prev_text[:2000]

                prompt = (
                    f"前一位模型（{prev_name}）的回复如下：\n\n"
                    f"---\n{prev_text}\n---\n\n"
                    f"请回应上述观点。如果你完全同意对方的答案，请在回复最开头写上 [AGREE]。"
                )

                curr_messages.append({"role": "user", "content": prompt})
                self.status_detail = f"第 {self.round} 轮 — {curr_name} 思考中..."
                self._emit("round_start", {"round": self.round, "model": curr_name})
                response = await self._stream_model(curr_client, curr_messages, curr_name, self.round)
                if self._stop_flag:
                    self._finish_stopped(); return

                curr_messages.append({"role": "assistant", "content": response})

                # Always update latest_responses (even if filtered from history)
                latest_responses[curr_name] = response

                # Diversity retention check before adding to history
                if self.diversity_retention:
                    if not await self._is_diverse_enough(response):
                        self._emit("round_end", {"round": self.round, "model": curr_name, "agreed": False, "filtered": True})
                        turn_idx = (turn_idx + 1) % 3
                        continue

                self.history.append({"round": self.round, "model": curr_name, "content": response})
                agreed = self._check_agreement(response)
                self._emit("round_end", {"round": self.round, "model": curr_name, "agreed": agreed})

                # Check consensus: if 2 of last 3 responses agree
                recent = list(latest_responses.values())
                if sum(1 for r in recent if self._check_agreement(r)) >= 2:
                    self._finish("consensus", "达成共识"); return

                turn_idx = (turn_idx + 1) % 3

            if not self._stop_flag:
                self.status = "max_rounds"
                self.status_detail = "正在生成报告..."
                self._emit("debate_end", {"status": "max_rounds", "rounds": self.round})
                await self._generate_report()
                self.save_to_disk()

        except Exception as e:
            self._finish_error(e)


    async def _apply_memory_masking(self, messages: list[dict]) -> list[dict]:
        if not self.masking_model:
            return messages
        system = messages[0] if messages and messages[0]["role"] == "system" else None
        conversation = messages[1:] if system else messages
        if len(conversation) < 4:
            return messages
        transcript = "\n".join(
            f"{'用户' if m['role']=='user' else '助手'}: {m['content'][:800]}" for m in conversation
        )
        mask_prompt = (
            f"以下是辩论对话的一部分。请提取关键论点和论据，去除已被反驳的错误推导，"
            f"输出精简摘要（不超过500字）：\n\n{transcript}"
        )
        try:
            summary = await self.masking_model.chat([{"role": "user", "content": mask_prompt}])
            return ([system] if system else []) + [{"role": "user", "content": f"以下是之前讨论的关键论点摘要：\n\n{summary}"}]
        except Exception:
            return messages

    async def _is_diverse_enough(self, new_response: str) -> bool:
        if not self.embedding_url or not self.history:
            return True
        recent_texts = [e["content"] for e in self.history[-4:] if e["content"]]
        if not recent_texts:
            return True
        try:
            import httpx
            texts = recent_texts + [new_response]
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(
                    f"{self.embedding_url.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {self.embedding_key}", "Content-Type": "application/json"},
                    json={"model": self.embedding_model, "input": texts},
                )
                if resp.status_code != 200:
                    return True
                data = resp.json()
                embeddings = [e["embedding"] for e in data["data"]]
                new_emb = embeddings[-1]
                for old_emb in embeddings[:-1]:
                    sim = self._cosine_sim(new_emb, old_emb)
                    if sim > 0.88:
                        return False
        except Exception:
            pass
        return True

    @staticmethod
    def _cosine_sim(a: list, b: list) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0
        return dot / (na * nb)

    def _finish_stopped(self):
        self.status = "stopped"
        self.status_detail = "已手动停止"
        self._emit("debate_end", {"status": "stopped", "rounds": self.round})
        self.save_to_disk()

    def _finish(self, status: str, detail: str):
        self.status = status
        self.status_detail = detail
        self._emit("debate_end", {"status": status, "rounds": self.round})
        self.save_to_disk()

    def _finish_error(self, e: Exception):
        self.status = "error"
        self.status_detail = f"出错: {str(e)[:50]}"
        self.error_message = str(e)
        self._emit("error", {"message": str(e)})
        self.save_to_disk()

    async def _stream_model(self, client: LLMClient, messages: list[dict], name: str, rnd: int) -> str:
        self._emit("content_start", {"round": rnd, "model": name})
        thinking_chars = 0
        parts = []
        async for token in client.chat_stream(messages):
            if self._stop_flag:
                return "".join(parts)
            if is_thinking_token(token):
                thinking_chars += len(strip_thinking_prefix(token))
                self._emit("thinking", {"content": strip_thinking_prefix(token)})
            else:
                parts.append(token)
                self._emit("token", {"content": token})
        response_text = "".join(parts)
        prompt_est = LLMClient.estimate_messages_tokens(messages)
        completion_est = LLMClient.estimate_tokens(response_text)
        thinking_est = thinking_chars // 3
        self._update_token_usage(name, prompt_est, completion_est, thinking_est)
        self._emit("content_end", {})
        return response_text

    def _update_token_usage(self, name: str, prompt: int, completion: int, thinking: int):
        if name not in self.token_usage:
            self.token_usage[name] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0, "calls": 0}
        u = self.token_usage[name]
        u["prompt"] += prompt
        u["completion"] += completion
        u["thinking"] += thinking
        u["total"] = u["prompt"] + u["completion"] + u["thinking"]
        u["calls"] += 1
        total_all = sum(v["total"] for v in self.token_usage.values())
        self._emit("token_usage", {**self.token_usage, "_total": total_all})

    async def _generate_report(self):
        model_desc = "多个AI模型" if self.mode == "chain3" else "两个AI模型"
        report_prompt = (
            f"以下{model_desc}就「{self.topic}」进行了 {self.round} 轮讨论，但未能达成共识。\n\n"
            f"请整理出一份分歧分析报告，要求：\n"
            f"1. 列出模型**达成共识的要点**（一致认可的部分）\n"
            f"2. 列出**核心分歧点**（持不同意见的部分），并分别概述每方的主要论点和论据\n"
            f"3. 对每个分歧点给出你认为是正确的判断，并说明理由\n"
            f"4. 标注关键结论和建议\n\n"
            f"讨论记录：\n\n"
        )
        for entry in self.history:
            report_prompt += f"## 第 {entry['round']} 轮 — {entry['model']}\n\n{entry['content']}\n\n"

        self._emit("report_start", {"model": self.name_a})
        parts = []
        try:
            async for token in self.model_a.chat_stream([{"role": "user", "content": report_prompt}]):
                if not is_thinking_token(token):
                    parts.append(token)
                    self._emit("report_token", {"content": token})
        except Exception:
            pass
        self.report = "".join(parts)
        self.report_model = self.name_a
        self._emit("report_end", {"model": self.name_a})

    def stop(self):
        self._stop_flag = True

    def get_events(self) -> asyncio.Queue:
        return self._event_queue

    def clear_queue(self):
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def save_markdown(self) -> str:
        CONVERSATIONS_DIR.mkdir(exist_ok=True)
        status_map = {
            "consensus": "达成共识",
            "disagreement": "存在分歧（超时停止）",
            "stopped": "手动停止",
            "max_rounds": "达到最大轮数",
            "error": "出错",
        }
        mode_map = {"sequential": "交替审查", "blind": "独立辩论", "chain3": "三人链式辩论"}
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# AI Debate: {self.topic}",
            "",
            f"**日期**: {timestamp}",
            f"**模式**: {mode_map.get(self.mode, self.mode)}",
            f"**模型 A**: {self.name_a}",
            f"**模型 B**: {self.name_b}",
        ]
        if self.mode == "chain3" and self.name_c:
            lines.append(f"**模型 C**: {self.name_c}")
        lines.extend([
            f"**结果**: {status_map.get(self.status, self.status)}",
            f"**总轮数**: {self.round}",
            "",
            "---",
            "",
        ])
        for entry in self.history:
            lines.append(f"## 第 {entry['round']} 轮 - {entry['model']}")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
            lines.append("---")
            lines.append("")

        content = "\n".join(lines)
        filename = f"{self.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        (CONVERSATIONS_DIR / filename).write_text(content, encoding="utf-8")
        return content


class _DebateEvent:
    def __init__(self, event_type: str, data: dict):
        self.type = event_type
        self.data = data

    def to_sse(self) -> str:
        import json
        return f"event: {self.type}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"
