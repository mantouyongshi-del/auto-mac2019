import sys
# 不要把当前目录放在第一位，避免覆盖标准库email
sys.path.insert(1, '/app/laya-model')

print("1. 加载laya模型...")
from agent import load
agent = load('convaiinnovations/laya', device='cpu')
print("加载完成！")

print("\n2. 测试第一个场景：回答质量评分")
# 测试一个简单的输入
result = agent.predict(
    state="用户在问一个事实性问题",
    questions=["2026年全球新能源汽车销量排名第一的是哪个品牌？"],
    model="typed-decisions"
)
print(f"输出结果: {result}")
