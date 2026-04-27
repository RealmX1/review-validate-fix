目前该项目尚未被分发；一切对于Review-Validate-Fix本身的backward compatibility work都应该在commit前被清理；
- 如果该工作是通过直接改动主程序达成，那么需要明确注明该backward compatiblity work的改动，并在验证了已完成任务后清理并log入已被gitignore的`dev_backward_compatibility`folder。

commit风格应遵循conventional commit

当某当前session先前已阅读文件出现超出预期的更改，可能是由其他agent进行的。对此情形默认行为是保留其变动。
- 如果该变动与你已经进行或计划进行的变动完全或部分重合，分析并自行决定是否进行进一步修改。
- 如果存在冲突部分，
  - 若你的计划是由开发者明确声明的任务，分析影响并将冲突部分覆盖；
  - 若非如此，将你的计划以及依赖与其的计划搁置并在未来回复中告知开发者。