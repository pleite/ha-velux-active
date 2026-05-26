# >>> velux-debug INJECTION 2: HashSignKey as hex
    new-instance v13, Ljava/math/BigInteger;
    const/4 v14, 0x1
    invoke-direct {v13, v14, v10}, Ljava/math/BigInteger;-><init>(I[B)V
    const/16 v14, 0x10
    invoke-virtual {v13, v14}, Ljava/math/BigInteger;->toString(I)Ljava/lang/String;
    move-result-object v13
    new-instance v14, Ljava/lang/StringBuilder;
    invoke-direct {v14}, Ljava/lang/StringBuilder;-><init>()V
    const-string v15, "HashSignKey: "
    invoke-virtual {v14, v15}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v14, v13}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v14}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v13
    const-string v14, "velux-debug"
    invoke-static {v14, v13}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I
    # <<< end injection 2
